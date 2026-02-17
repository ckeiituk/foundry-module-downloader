import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundry_module_fetch import (
    ensure_not_html_download,
    extract_gdrive_file_id,
    filename_from_cd,
    is_dropbox,
    is_google_drive,
    is_mega,
    is_probably_html_file,
    is_telegram,
    is_yandex_disk,
    normalize_dropbox_url,
    parse_mega_link,
    parse_telegram_message_url,
    parse_yandex_public_url,
)


class TestUrlDetection:
    def test_google_drive_file(self):
        assert is_google_drive("https://drive.google.com/file/d/abc/view")

    def test_google_docs(self):
        assert is_google_drive("https://docs.google.com/spreadsheets/d/abc")

    def test_not_google_drive(self):
        assert not is_google_drive("https://example.com/drive.google.com")

    def test_mega_nz(self):
        assert is_mega("https://mega.nz/file/abc#key")

    def test_mega_co_nz(self):
        assert is_mega("https://mega.co.nz/#F!abc!key")

    def test_not_mega(self):
        assert not is_mega("https://example.com/mega")

    def test_dropbox(self):
        assert is_dropbox("https://www.dropbox.com/s/abc/file.zip")

    def test_dropboxusercontent(self):
        assert is_dropbox("https://dl.dropboxusercontent.com/s/abc/file.zip")

    def test_not_dropbox(self):
        assert not is_dropbox("https://example.com/dropbox.com/s/abc")

    def test_yandex_ru(self):
        assert is_yandex_disk("https://disk.yandex.ru/d/abc")

    def test_yandex_yadi_sk(self):
        assert is_yandex_disk("https://yadi.sk/d/abc")

    def test_not_yandex(self):
        assert not is_yandex_disk("https://mail.yandex.ru/")

    def test_telegram_t_me(self):
        assert is_telegram("https://t.me/channel/123")

    def test_telegram_me(self):
        assert is_telegram("https://telegram.me/user/456")

    def test_not_telegram(self):
        assert not is_telegram("https://example.com/t.me/abc")


class TestGoogleDriveFileId:
    def test_standard_url(self):
        url = "https://drive.google.com/file/d/1aBcDeFg_hijKLMnop/view"
        assert extract_gdrive_file_id(url) == "1aBcDeFg_hijKLMnop"

    def test_uc_url(self):
        url = "https://drive.google.com/uc?export=download&id=1aBcDeFg_hijKLMnop"
        assert extract_gdrive_file_id(url) == "1aBcDeFg_hijKLMnop"

    def test_query_id(self):
        url = "https://drive.google.com/open?id=XYZ123"
        assert extract_gdrive_file_id(url) == "XYZ123"

    def test_folder_returns_none(self):
        assert extract_gdrive_file_id("https://drive.google.com/drive/folders/abc") is None


class TestMegaLink:
    def test_file_link(self):
        info = parse_mega_link("https://mega.nz/file/FILEID#KEY")
        assert info is not None
        assert info["kind"] == "file"
        assert info["file_id"] == "FILEID"
        assert info["key"] == "KEY"

    def test_folder_link(self):
        info = parse_mega_link("https://mega.nz/folder/FOLDERID#KEY")
        assert info is not None
        assert info["kind"] == "folder"
        assert info["folder_id"] == "FOLDERID"

    def test_folder_file_link(self):
        info = parse_mega_link("https://mega.nz/folder/FOLDERID#KEY/file/FILEID")
        assert info is not None
        assert info["kind"] == "folder_file"
        assert info["folder_id"] == "FOLDERID"
        assert info["file_id"] == "FILEID"

    def test_legacy_file(self):
        info = parse_mega_link("https://mega.nz/#!FILEID!KEY")
        assert info is not None
        assert info["kind"] == "file"
        assert info["file_id"] == "FILEID"

    def test_legacy_folder(self):
        info = parse_mega_link("https://mega.nz/#F!FOLDERID!KEY")
        assert info is not None
        assert info["kind"] == "folder"

    def test_non_mega(self):
        assert parse_mega_link("https://example.com/file") is None


class TestTelegramUrl:
    def test_public_channel(self):
        info = parse_telegram_message_url("https://t.me/mychannel/42")
        assert info is not None
        assert info["peer"] == "mychannel"
        assert info["msg_id"] == 42

    def test_private_channel(self):
        info = parse_telegram_message_url("https://t.me/c/1234567890/99")
        assert info is not None
        assert info["peer"] == -1001234567890
        assert info["msg_id"] == 99

    def test_public_preview(self):
        info = parse_telegram_message_url("https://t.me/s/mychannel/7")
        assert info is not None
        assert info["peer"] == "mychannel"
        assert info["msg_id"] == 7

    def test_non_telegram(self):
        assert parse_telegram_message_url("https://example.com/t.me/abc/1") is None

    def test_missing_msg_id(self):
        assert parse_telegram_message_url("https://t.me/mychannel") is None


class TestDropboxNormalize:
    def test_adds_dl_param(self):
        result = normalize_dropbox_url("https://www.dropbox.com/s/abc/file.zip?dl=0")
        assert "dl=1" in result

    def test_direct_url_unchanged(self):
        url = "https://dl.dropboxusercontent.com/s/abc/file.zip"
        assert normalize_dropbox_url(url) == url

    def test_non_dropbox_unchanged(self):
        url = "https://example.com/file.zip"
        assert normalize_dropbox_url(url) == url

    def test_no_existing_params(self):
        result = normalize_dropbox_url("https://www.dropbox.com/s/abc/file.zip")
        assert "dl=1" in result


class TestYandexParsing:
    def test_simple_d_link(self):
        url = "https://disk.yandex.ru/d/HASH123"
        public_url, path = parse_yandex_public_url(url)
        assert "HASH123" in public_url
        assert path is None

    def test_link_with_subpath(self):
        url = "https://disk.yandex.ru/d/HASH123/subdir/file.zip"
        public_url, path = parse_yandex_public_url(url)
        assert path == "/subdir/file.zip"

    def test_query_path_param(self):
        url = "https://disk.yandex.ru/public?hash=HASH123&path=/file.zip"
        _, path = parse_yandex_public_url(url)
        assert path == "/file.zip"


class TestFilenameFromCd:
    def test_rfc5987(self):
        assert filename_from_cd("attachment; filename*=UTF-8''my%20file.zip") == "my file.zip"

    def test_quoted(self):
        assert filename_from_cd('attachment; filename="archive.zip"') == "archive.zip"

    def test_unquoted(self):
        assert filename_from_cd("attachment; filename=archive.zip") == "archive.zip"

    def test_none(self):
        assert filename_from_cd(None) is None

    def test_empty(self):
        assert filename_from_cd("") is None


class TestHtmlGuard:
    def test_doctype_html(self, tmp_path):
        f = tmp_path / "file.zip"
        f.write_text("<!DOCTYPE html><html><body>Error</body></html>")
        assert is_probably_html_file(f)

    def test_html_tag(self, tmp_path):
        f = tmp_path / "file.zip"
        f.write_text("<html><head></head><body></body></html>")
        assert is_probably_html_file(f)

    def test_html_extension(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("anything")
        assert is_probably_html_file(f)

    def test_zip_magic_not_html(self, tmp_path):
        f = tmp_path / "file.zip"
        f.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        assert not is_probably_html_file(f)

    def test_ensure_raises_on_html(self, tmp_path):
        f = tmp_path / "module.zip"
        f.write_text("<!DOCTYPE html><html><body>Access Denied</body></html>")
        with pytest.raises(RuntimeError, match="HTML"):
            ensure_not_html_download(f, "TestSource", "https://example.com")

    def test_ensure_passes_on_binary(self, tmp_path):
        f = tmp_path / "module.zip"
        f.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        ensure_not_html_download(f, "TestSource", "https://example.com")  # no raise
