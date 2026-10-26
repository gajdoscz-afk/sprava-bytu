"""Převod Word dokumentů do PDF přes LibreOffice (soffice)."""
import os
import subprocess
import tempfile
from pathlib import Path


class PdfError(Exception):
    pass


def docx_to_pdf(data: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "dokument.docx"
        src.write_bytes(data)
        profile = Path(tmp) / "profil"  # LibreOffice potřebuje vlastní zapisovatelný profil
        cmd = ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
               "--convert-to", "pdf", "--outdir", tmp, str(src)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120,
                           env={**os.environ, "HOME": tmp})
        except FileNotFoundError:
            raise PdfError("Na serveru není nainstalovaný LibreOffice")
        except (subprocess.SubprocessError, OSError):
            raise PdfError("Převod do PDF se nepodařil")
        out = Path(tmp) / "dokument.pdf"
        if not out.exists():
            raise PdfError("Převod do PDF se nepodařil")
        return out.read_bytes()
