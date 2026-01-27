#!/usr/bin/env python3
import argparse
import importlib.util
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_MODULES_DIR = Path("/opt/foundry/data/Data/modules")
DEFAULT_OWNER = "foundry:foundry-files"

ARCHIVE_TAR_EXTS = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz",
    ".tbz2",
    ".tar.xz",
    ".txz",
)
ARCHIVE_7Z_EXTS = (".7z", ".rar")


def run(
    cmd: List[str],
    ok_codes: Iterable[int] = (0,),
    stdout: Optional[int] = None,
    stderr: Optional[int] = None,
) -> None:
    result = subprocess.run(cmd, stdout=stdout, stderr=stderr)
    if result.returncode not in ok_codes:
        raise subprocess.CalledProcessError(result.returncode, cmd)


def ensure_module(module_name: str, pip_name: str) -> None:
    if importlib.util.find_spec(module_name) is not None:
        return

    base_cmd = [sys.executable, "-m", "pip", "install", pip_name]
    tried: List[List[str]] = []

    if site.ENABLE_USER_SITE:
        cmd = base_cmd + ["--user"]
        tried.append(cmd)
        try:
            run(cmd)
        except subprocess.CalledProcessError:
            pass
        else:
            importlib.invalidate_caches()
            if importlib.util.find_spec(module_name) is not None:
                return

    cmd = base_cmd + ["--break-system-packages"]
    tried.append(cmd)
    run(cmd)
    importlib.invalidate_caches()
    if importlib.util.find_spec(module_name) is None:
        joined = " | ".join(" ".join(c) for c in tried)
        raise RuntimeError(f"Failed to install Python module '{module_name}'. Tried: {joined}")


def is_google_drive(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("drive.google.com") or host.endswith("docs.google.com")


def is_mega(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("mega.nz") or host.endswith("mega.co.nz")


def is_dropbox(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("dropbox.com") or host.endswith("dropboxusercontent.com")


def normalize_mega_url(url: str) -> str:
    if not is_mega(url):
        return url

    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    fragment = parsed.fragment
    scheme = parsed.scheme or "https"
    base = f"{scheme}://{parsed.netloc}"

    if len(parts) >= 2 and fragment:
        node_type = parts[0].lower()
        node_id = parts[1]
        if node_type == "file":
            return f"{base}/#!{node_id}!{fragment}"
        if node_type == "folder" and len(parts) == 2:
            return f"{base}/#F!{node_id}!{fragment}"

    return url


def normalize_dropbox_url(url: str) -> str:
    if not is_dropbox(url):
        return url

    parsed = urlparse(url)
    if parsed.netloc.lower().endswith("dropboxusercontent.com"):
        return url

    params = parse_qs(parsed.query)
    params["dl"] = ["1"]
    query = "&".join(f"{key}={value[0]}" for key, value in params.items())
    return parsed._replace(query=query).geturl()


def extract_gdrive_file_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.query:
        params = parse_qs(parsed.query)
        if "id" in params and params["id"]:
            return params["id"][0]
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
    if match:
        return match.group(1)
    match = re.search(r"/uc\?export=download&id=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return None


def get_confirm_token_from_html(html: str) -> Optional[str]:
    patterns = [
        r"confirm=([0-9A-Za-z_-]+)",
        r'name="confirm"\s+value="([^"]+)"',
        r"'confirm'\s*:\s*'([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def extract_download_url_from_html(html: str) -> Optional[str]:
    match = re.search(r'"downloadUrl"\s*:\s*"([^"]+)"', html)
    if match:
        url = match.group(1)
        url = url.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
        return url

    match = re.search(r'href="(/uc\?export=download[^"]+)"', html)
    if match:
        return "https://drive.google.com" + match.group(1).replace("&amp;", "&")

    return None


def extract_gdrive_form_action(html: str) -> Optional[str]:
    match = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"', html)
    if match:
        return match.group(1).replace("&amp;", "&")
    return None


def extract_gdrive_form_params(html: str) -> dict:
    params: dict = {}
    for name in ("confirm", "uuid", "id", "export"):
        match = re.search(rf'name="{name}"\s+value="([^"]+)"', html)
        if match:
            params[name] = match.group(1)
    return params


def extract_gdrive_action_params(html: str) -> dict:
    match = re.search(r'action="([^"]+)"', html)
    if not match:
        return {}
    action = match.group(1).replace("&amp;", "&")
    parsed = urlparse(action)
    params = parse_qs(parsed.query)
    return {key: values[0] for key, values in params.items() if values}


def filename_from_cd(content_disposition: Optional[str]) -> Optional[str]:
    if not content_disposition:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition)
    if match:
        return unquote(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', content_disposition)
    if match:
        return match.group(1)
    return None


def save_debug_html(html: str, debug_dir: Optional[Path], stem: str) -> Optional[Path]:
    if debug_dir is None:
        return None
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_") or "gdrive"
    debug_path = debug_dir / f"{safe_stem}.html"
    debug_path.write_text(html, encoding="utf-8", errors="replace")
    return debug_path


def extract_html_title(html: str) -> Optional[str]:
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\\s+", " ", match.group(1)).strip()
    return title or None


def download_google_drive(
    url: str, dest_dir: Path, debug_dir: Optional[Path]
) -> List[Path]:
    ensure_module("requests", "requests")
    import requests  # type: ignore

    file_id = extract_gdrive_file_id(url)
    if not file_id:
        raise RuntimeError(f"Could not parse Google Drive file ID from URL: {url}")

    session = requests.Session()
    base_url = "https://drive.google.com/uc?export=download"

    response = session.get(base_url, params={"id": file_id}, stream=True)
    token = None
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            token = value
            break

    if token is None:
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            html = response.text
            download_url = extract_download_url_from_html(html)
            token = get_confirm_token_from_html(html)
            form_action = extract_gdrive_form_action(html)
            form_params = extract_gdrive_form_params(html)
            action_params = extract_gdrive_action_params(html)
            response.close()
            if download_url:
                response = session.get(download_url, stream=True)
                response.raise_for_status()
                token = None
            elif form_params or action_params:
                merged = {"id": file_id}
                merged.update(action_params)
                merged.update(form_params)
                target_url = form_action or base_url
                response = session.get(target_url, params=merged, stream=True)
                response.raise_for_status()
                token = None

    if token:
        response.close()
        response = session.get(
            base_url, params={"id": file_id, "confirm": token}, stream=True
        )

    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type and not response.headers.get("content-disposition"):
        html = response.text
        title = extract_html_title(html)
        debug_path = save_debug_html(html, debug_dir, f"gdrive_{file_id}")
        if "accounts.google.com" in html or "ServiceLogin" in html:
            raise RuntimeError(
                "Google Drive требует авторизацию. Сделайте файл публичным "
                "(Anyone with the link) и попробуйте снова."
            )
        hint = f" (страница: {title})" if title else ""
        debug_hint = f" HTML сохранен в {debug_path}" if debug_path else ""
        raise RuntimeError(
            "Google Drive returned HTML instead of a file. "
            f"Check sharing permissions.{hint}{debug_hint}"
        )
    filename = filename_from_cd(response.headers.get("content-disposition"))
    if not filename:
        filename = f"{file_id}"

    filename = Path(filename).name
    target = dest_dir / filename
    with target.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
    return [target]


def filename_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return unquote(name) if name else None


def download_dropbox(url: str, dest_dir: Path) -> List[Path]:
    ensure_module("requests", "requests")
    import requests  # type: ignore

    download_url = normalize_dropbox_url(url)
    response = requests.get(download_url, stream=True, allow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type and not response.headers.get("content-disposition"):
        raise RuntimeError(
            "Dropbox returned HTML instead of a file. Check sharing permissions."
        )

    filename = filename_from_cd(response.headers.get("content-disposition"))
    if not filename:
        filename = filename_from_url(response.url) or filename_from_url(download_url)
    if not filename:
        raise RuntimeError("Could not determine Dropbox filename.")

    filename = Path(filename).name
    target = dest_dir / filename
    with target.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
    return [target]


def download_mega(url: str, dest_dir: Path) -> List[Path]:
    mega_url = normalize_mega_url(url)

    if shutil.which("mega-get"):
        run(["mega-get", mega_url, str(dest_dir)])
        items = list(dest_dir.iterdir())
        if not items:
            raise RuntimeError("Mega download did not create any files.")
        return items

    if shutil.which("megadl"):
        run(["megadl", "--path", str(dest_dir), mega_url])
        items = list(dest_dir.iterdir())
        if not items:
            raise RuntimeError("Mega download did not create any files.")
        return items

    if sys.version_info >= (3, 13):
        raise RuntimeError(
            "Python 3.13 is not compatible with mega.py. "
            "Install MegaCMD (mega-get) or megatools (megadl), "
            "or use Python 3.12/3.11."
        )

    ensure_module("mega", "mega.py")
    from mega import Mega  # type: ignore

    mega = Mega()
    try:
        mega.download_url(url, str(dest_dir))
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Mega download failed: {exc}") from exc

    items = list(dest_dir.iterdir())
    if not items:
        raise RuntimeError("Mega download did not create any files.")
    return items


def detect_archive(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(ARCHIVE_TAR_EXTS):
        return "tar"
    if name.endswith(ARCHIVE_7Z_EXTS):
        return "7z"
    return None


def extract_archive(archive_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    kind = detect_archive(archive_path)
    if kind == "zip":
        # unzip returns 1 for warnings (e.g., filename encoding). Treat as success if files extracted.
        try:
            run(
                ["unzip", "-qq", "-o", str(archive_path), "-d", str(dest_dir)],
                ok_codes=(0, 1),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            # Fallback to 7z if unzip fails hard.
            run(["7z", "x", "-y", f"-o{dest_dir}", str(archive_path)])
    elif kind == "tar":
        run(["tar", "-xf", str(archive_path), "-C", str(dest_dir)])
    elif kind == "7z":
        run(["7z", "x", "-y", f"-o{dest_dir}", str(archive_path)])
    else:
        raise RuntimeError(f"Unsupported archive type: {archive_path}")
    if not any(dest_dir.iterdir()):
        raise RuntimeError(f"Archive extraction produced no files: {archive_path}")


def move_or_merge(src: Path, dest_root: Path, force: bool) -> Path:
    dest = dest_root / src.name
    if not dest.exists():
        shutil.move(str(src), str(dest))
        return dest

    if src.is_dir() and dest.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
        shutil.rmtree(src)
        return dest

    if src.is_file() and dest.is_file():
        dest.unlink()
        shutil.move(str(src), str(dest))
        return dest

    if force:
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
        shutil.move(str(src), str(dest))
        return dest

    raise RuntimeError(
        f"Destination exists with different type: {dest}. Use --force to override."
    )


def chown_paths(paths: Iterable[Path], owner: str) -> None:
    for path in paths:
        run(["chown", "-R", owner, str(path)])


def process_downloads(
    downloaded: List[Path], modules_dir: Path, force: bool
) -> List[Path]:
    moved: List[Path] = []
    for item in downloaded:
        if item.is_dir():
            moved.append(move_or_merge(item, modules_dir, force))
            continue

        kind = detect_archive(item)
        if kind:
            with tempfile.TemporaryDirectory(prefix="foundry_extract_") as extract_tmp:
                extract_dir = Path(extract_tmp)
                extract_archive(item, extract_dir)
                extracted_items = list(extract_dir.iterdir())
                if not extracted_items:
                    raise RuntimeError(f"Archive is empty: {item}")
                for extracted in extracted_items:
                    moved.append(move_or_merge(extracted, modules_dir, force))
        else:
            moved.append(move_or_merge(item, modules_dir, force))

    return moved


def download_url(
    url: str, dest_dir: Path, debug_dir: Optional[Path]
) -> List[Path]:
    if is_google_drive(url):
        return download_google_drive(url, dest_dir, debug_dir)
    if is_dropbox(url):
        return download_dropbox(url, dest_dir)
    if is_mega(url):
        return download_mega(url, dest_dir)
    raise RuntimeError(f"Unsupported URL: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download Foundry modules from Google Drive or Mega links, extract archives, "
            "and set ownership."
        )
    )
    parser.add_argument("url", nargs="+", help="Google Drive or Mega URL")
    parser.add_argument(
        "--modules-dir",
        default=str(DEFAULT_MODULES_DIR),
        help=f"Foundry modules directory (default: {DEFAULT_MODULES_DIR})",
    )
    parser.add_argument(
        "--debug-html",
        default="",
        help="Directory to save HTML responses from Google Drive on error",
    )
    parser.add_argument(
        "--owner",
        default=DEFAULT_OWNER,
        help=f"Owner to apply after extraction (default: {DEFAULT_OWNER})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing module files if name conflicts",
    )

    args = parser.parse_args()
    modules_dir = Path(args.modules_dir)
    modules_dir.mkdir(parents=True, exist_ok=True)

    debug_dir = Path(args.debug_html).expanduser() if args.debug_html else None
    all_moved: List[Path] = []
    for url in args.url:
        with tempfile.TemporaryDirectory(prefix="foundry_download_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            downloaded = download_url(url, tmp_path, debug_dir)
            moved = process_downloads(downloaded, modules_dir, args.force)
            all_moved.extend(moved)

    if all_moved:
        chown_paths(all_moved, args.owner)
        for path in all_moved:
            print(f"Installed: {path}")
    else:
        print("Nothing installed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
