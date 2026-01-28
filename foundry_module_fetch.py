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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional
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
TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog")


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    session: str


def load_dotenv(path: Path, override: bool = False) -> bool:
    if not path.is_file():
        return False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value

    return True


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


def get_tqdm() -> Optional[Callable[..., object]]:
    try:
        ensure_module("tqdm", "tqdm")
        from tqdm import tqdm  # type: ignore
    except Exception as exc:
        print(f"Warning: tqdm unavailable ({exc}). Progress disabled.", file=sys.stderr)
        return None
    return tqdm


def is_google_drive(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("drive.google.com") or host.endswith("docs.google.com")


def is_mega(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("mega.nz") or host.endswith("mega.co.nz")


def is_dropbox(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("dropbox.com") or host.endswith("dropboxusercontent.com")


def normalize_telegram_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url

    path = parsed.path.lstrip("/")
    for host in TELEGRAM_HOSTS:
        if path.startswith(f"{host}/"):
            return f"https://{path}"
    return url


def is_telegram(url: str) -> bool:
    normalized = normalize_telegram_url(url)
    host = urlparse(normalized).netloc.lower()
    if host in TELEGRAM_HOSTS:
        return True
    return any(host.endswith(f".{candidate}") for candidate in TELEGRAM_HOSTS)


def parse_telegram_message_url(url: str) -> Optional[dict]:
    normalized = normalize_telegram_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    if host not in TELEGRAM_HOSTS and not any(
        host.endswith(f".{candidate}") for candidate in TELEGRAM_HOSTS
    ):
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    if parts[0] == "s":
        if len(parts) < 3:
            return None
        username = parts[1]
        msg_id = parts[-1]
        if not msg_id.isdigit():
            return None
        return {"peer": username, "msg_id": int(msg_id)}

    if parts[0] == "c":
        if len(parts) < 3:
            return None
        chat_id = parts[1]
        msg_id = parts[-1]
        if not chat_id.isdigit() or not msg_id.isdigit():
            return None
        return {"peer": int(f"-100{chat_id}"), "msg_id": int(msg_id)}

    if len(parts) < 2:
        return None
    username = parts[0]
    msg_id = parts[-1]
    if not msg_id.isdigit():
        return None
    return {"peer": username, "msg_id": int(msg_id)}


def parse_mega_link(url: str) -> Optional[dict]:
    if not is_mega(url):
        return None

    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    fragment = parsed.fragment
    scheme = parsed.scheme or "https"
    base = f"{scheme}://{parsed.netloc}"

    if len(parts) >= 2 and parts[0].lower() == "file":
        return {
            "kind": "file",
            "file_id": parts[1],
            "key": fragment,
            "base": base,
        }

    if len(parts) >= 2 and parts[0].lower() == "folder":
        folder_id = parts[1]
        if "/file/" in fragment:
            key, _, file_id = fragment.partition("/file/")
            return {
                "kind": "folder_file",
                "folder_id": folder_id,
                "file_id": file_id,
                "key": key,
                "base": base,
            }
        return {
            "kind": "folder",
            "folder_id": folder_id,
            "key": fragment,
            "base": base,
        }

    if fragment.startswith("F!"):
        tokens = fragment.split("!")
        if len(tokens) >= 3:
            info = {
                "kind": "folder",
                "folder_id": tokens[1],
                "key": tokens[2],
                "base": base,
            }
            if len(tokens) >= 4 and tokens[3]:
                info["kind"] = "folder_file"
                info["file_id"] = tokens[3]
            return info

    if fragment.startswith("!"):
        tokens = fragment.split("!")
        if len(tokens) >= 3:
            return {
                "kind": "file",
                "file_id": tokens[1],
                "key": tokens[2],
                "base": base,
            }

    return None


def mega_url_for_megatools(url: str) -> tuple[str, bool]:
    info = parse_mega_link(url)
    if not info:
        return url, False

    base = info["base"]
    kind = info["kind"]
    if kind == "file":
        file_id = info.get("file_id", "")
        key = info.get("key", "")
        if file_id and key:
            return f"{base}/#!{file_id}!{key}", False
    if kind == "folder":
        folder_id = info.get("folder_id", "")
        key = info.get("key", "")
        if folder_id and key:
            return f"{base}/#F!{folder_id}!{key}", False
    if kind == "folder_file":
        folder_id = info.get("folder_id", "")
        key = info.get("key", "")
        if folder_id and key:
            return f"{base}/#F!{folder_id}!{key}", True

    return url, False


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


def write_stream_to_file(
    response,
    target: Path,
    desc: str,
    progress: bool,
) -> None:
    total = None
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            total_value = int(content_length)
        except ValueError:
            total_value = 0
        total = total_value or None

    tqdm = get_tqdm() if progress else None
    with target.open("wb") as handle:
        if tqdm:
            with tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=desc,
            ) as bar:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        handle.write(chunk)
                        bar.update(len(chunk))
        else:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)


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
    url: str, dest_dir: Path, debug_dir: Optional[Path], progress: bool
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
    write_stream_to_file(response, target, filename, progress)
    return [target]


def filename_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return unquote(name) if name else None


def download_dropbox(url: str, dest_dir: Path, progress: bool) -> List[Path]:
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
    write_stream_to_file(response, target, filename, progress)
    return [target]


def download_mega(url: str, dest_dir: Path) -> List[Path]:
    if shutil.which("mega-get"):
        run(["mega-get", url, str(dest_dir)])
        items = list(dest_dir.iterdir())
        if not items:
            raise RuntimeError("Mega download did not create any files.")
        return items

    if shutil.which("megadl"):
        info = parse_mega_link(url)
        if info and info.get("kind") == "folder_file":
            try:
                run(["megadl", "--path", str(dest_dir), url])
            except subprocess.CalledProcessError:
                mega_url, folder_fallback = mega_url_for_megatools(url)
                if folder_fallback:
                    print(
                        "Warning: Mega folder/file link detected; megatools will "
                        "download the entire folder.",
                        file=sys.stderr,
                    )
                run(["megadl", "--path", str(dest_dir), mega_url])
        else:
            mega_url, folder_fallback = mega_url_for_megatools(url)
            if folder_fallback:
                print(
                    "Warning: Mega folder/file link detected; megatools will "
                    "download the entire folder.",
                    file=sys.stderr,
                )
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


def download_telegram(
    url: str, dest_dir: Path, config: TelegramConfig, progress: bool
) -> List[Path]:
    ensure_module("telethon", "telethon")
    from telethon.sync import TelegramClient  # type: ignore

    info = parse_telegram_message_url(url)
    if not info:
        raise RuntimeError(f"Unsupported Telegram message URL: {url}")

    with TelegramClient(config.session, config.api_id, config.api_hash) as client:
        client.start()
        message = client.get_messages(info["peer"], ids=info["msg_id"])
        if not message:
            raise RuntimeError("Telegram message not found or inaccessible.")
        if not message.media:
            raise RuntimeError("Telegram message has no media to download.")
        if progress:
            tqdm = get_tqdm()
        else:
            tqdm = None

        if tqdm and hasattr(client, "iter_download"):
            file_name = None
            if message.file:
                file_name = message.file.name or None
                if not file_name and message.file.ext:
                    file_name = f"telegram_{message.id}{message.file.ext}"
            if not file_name:
                file_name = f"telegram_{message.id}"

            target = dest_dir / Path(file_name).name
            total = message.file.size if message.file and message.file.size else None
            with tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc="Telegram",
            ) as bar:
                with target.open("wb") as handle:
                    for chunk in client.iter_download(message.media):
                        handle.write(chunk)
                        bar.update(len(chunk))
            result = str(target)
        else:
            result = client.download_media(
                message,
                file=str(dest_dir),
                progress_callback=None,
            )

    if not result:
        raise RuntimeError("Telegram download produced no files.")

    if isinstance(result, list):
        paths = [Path(item) for item in result if item]
    else:
        paths = [Path(result)]

    if not paths:
        raise RuntimeError("Telegram download produced no files.")
    return paths


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
    downloaded: List[Path],
    modules_dir: Path,
    force: bool,
    work_dir: Optional[Path],
) -> List[Path]:
    moved: List[Path] = []
    for item in downloaded:
        if item.is_dir():
            moved.append(move_or_merge(item, modules_dir, force))
            continue

        kind = detect_archive(item)
        if kind:
            with tempfile.TemporaryDirectory(
                prefix="foundry_extract_",
                dir=str(work_dir) if work_dir else None,
            ) as extract_tmp:
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
    url: str,
    dest_dir: Path,
    debug_dir: Optional[Path],
    telegram: Optional[TelegramConfig],
    progress: bool,
) -> List[Path]:
    if is_google_drive(url):
        return download_google_drive(url, dest_dir, debug_dir, progress)
    if is_dropbox(url):
        return download_dropbox(url, dest_dir, progress)
    if is_mega(url):
        return download_mega(url, dest_dir)
    if is_telegram(url):
        if telegram is None:
            raise RuntimeError(
                "Telegram URL detected but API credentials are missing. "
                "Provide --tg-api-id and --tg-api-hash (or TG_API_ID/TG_API_HASH)."
            )
        return download_telegram(url, dest_dir, telegram, progress)
    raise RuntimeError(f"Unsupported URL: {url}")


def parse_telegram_config(args: argparse.Namespace) -> Optional[TelegramConfig]:
    api_id = (
        args.tg_api_id
        if args.tg_api_id is not None
        else os.environ.get("TG_API_ID", "")
    )
    api_hash = (
        args.tg_api_hash
        if args.tg_api_hash is not None
        else os.environ.get("TG_API_HASH", "")
    )
    session = (
        args.tg_session
        if args.tg_session is not None
        else os.environ.get("TG_SESSION", "telegram.session")
    )

    if not api_id and not api_hash:
        return None
    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram credentials are incomplete. Provide both --tg-api-id "
            "and --tg-api-hash (or set TG_API_ID and TG_API_HASH)."
        )
    try:
        api_id_value = int(api_id)
    except ValueError as exc:
        raise RuntimeError("Telegram --tg-api-id must be a number.") from exc

    session_path = Path(session).expanduser()
    try:
        session_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot create Telegram session directory: {session_path.parent}"
        ) from exc

    return TelegramConfig(
        api_id=api_id_value,
        api_hash=api_hash,
        session=str(session_path),
    )


def main() -> int:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument(
        "--env-file",
        default=None,
        help="Path to a .env file with TG_API_ID/TG_API_HASH/TG_SESSION.",
    )
    env_parser.add_argument(
        "--no-env",
        action="store_true",
        help="Disable loading the .env file.",
    )
    env_args, _ = env_parser.parse_known_args()
    if not env_args.no_env:
        if env_args.env_file:
            load_dotenv(Path(env_args.env_file).expanduser())
        else:
            candidate_paths = [
                Path.cwd() / ".env",
                Path(__file__).resolve().parent / ".env",
                Path.home() / ".config" / "foundry-module-downloader" / ".env",
            ]
            for candidate in candidate_paths:
                if load_dotenv(candidate):
                    break

    parser = argparse.ArgumentParser(
        description=(
            "Download Foundry modules from Google Drive, Dropbox, Mega, or Telegram "
            "links, extract archives, and set ownership."
        )
    )
    parser.add_argument(
        "url",
        nargs="+",
        help="Google Drive, Dropbox, Mega, or Telegram message URL",
    )
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
    parser.add_argument(
        "--work-dir",
        default="",
        help=(
            "Directory for temporary downloads/extraction (default: system tmp). "
            "Useful if /tmp is small (tmpfs)."
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bars (still shows Mega CLI progress).",
    )
    parser.add_argument(
        "--tg-api-id",
        default=None,
        help="Telegram API ID (or set TG_API_ID). Required for Telegram URLs.",
    )
    parser.add_argument(
        "--tg-api-hash",
        default=None,
        help="Telegram API hash (or set TG_API_HASH). Required for Telegram URLs.",
    )
    parser.add_argument(
        "--tg-session",
        default=None,
        help=(
            "Telegram session file path (default: telegram.session or TG_SESSION). "
            "Created on first login and reused."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "Path to a .env file with TG_API_ID/TG_API_HASH/TG_SESSION. "
            "If omitted, looks in CWD, script directory, then "
            "~/.config/foundry-module-downloader/.env."
        ),
    )
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="Disable loading the .env file.",
    )

    args = parser.parse_args()
    modules_dir = Path(args.modules_dir)
    modules_dir.mkdir(parents=True, exist_ok=True)

    debug_dir = Path(args.debug_html).expanduser() if args.debug_html else None
    work_dir = Path(args.work_dir).expanduser() if args.work_dir else None
    if work_dir:
        work_dir.mkdir(parents=True, exist_ok=True)

    telegram = parse_telegram_config(args)
    progress_enabled = not args.no_progress

    all_moved: List[Path] = []
    for url in args.url:
        with tempfile.TemporaryDirectory(
            prefix="foundry_download_",
            dir=str(work_dir) if work_dir else None,
        ) as tmp_dir:
            tmp_path = Path(tmp_dir)
            downloaded = download_url(
                url,
                tmp_path,
                debug_dir,
                telegram,
                progress_enabled,
            )
            moved = process_downloads(downloaded, modules_dir, args.force, work_dir)
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
