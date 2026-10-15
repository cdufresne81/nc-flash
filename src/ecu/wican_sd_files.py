"""The ONE client for the WiCAN SD file manager's list + delete calls.

The firmware's ``sd_filemgr`` serves ``GET /files?op=list&path=<dir>`` and
``POST /files {"op":"delete","path":"<dir>/<name>"}`` on port 80, with paths
relative to ``/sdcard``. Trip-log cleanup (:mod:`src.ecu.wican_logs`) and the
staged-ROM retention (:mod:`src.ecu.wican_sd_roms`) both go through here.

Deletes are single files only: the file manager deletes a folder recursively,
so a delete path is always ``<dir>/<basename>`` built from a checked basename,
never a path from the device. The firmware refuses every delete with ``409``
while an ECU flash is running (it reads the staged image from the card).

Headless: standard library only, no PySide6.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

from .wican_http import (
    DEFAULT_TIMEOUT_S,
    WiCANHttpError,
    get_json,
    post_json,
    sanitize_basename,
)

logger = logging.getLogger(__name__)

#: The SD file manager endpoint (``sd_filemgr``); paths are relative to /sdcard.
FILES_PATH = "/files"

#: Names a delete may touch: the characters the firmware itself uses.
#: The download URL is query-encoded (a space becomes ``+``) and the firmware
#: never decodes it, while the delete path is sent verbatim — for any other
#: name the file downloaded and the file deleted could differ.
DELETABLE_NAME = re.compile(r"[A-Za-z0-9._-]+")


class WiCANSdFilesError(WiCANHttpError):
    """An SD file-manager listing or delete failed or was refused."""


class WiCANSdFiles:
    """List one SD folder and delete single files from it."""

    def __init__(
        self,
        host: str,
        http_port: int = 80,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        *,
        error=WiCANSdFilesError,
    ):
        """``error`` is the exception class raised on a malformed reply or
        refusal, so a caller keeps its own error type (trip logs raise
        ``WiCANLogsError``)."""
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s
        self._error = error

    def _url(self, query: Optional[dict] = None) -> str:
        url = f"http://{self.host}:{self.http_port}{FILES_PATH}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def list_dir(self, rel_dir: str) -> dict:
        """Fresh view of one folder: ``{name: entry}``.

        Each entry is the raw ``sd_filemgr`` item (``type``, ``size``,
        ``mtime``, and ``active`` / ``locked`` flags when set). An unmounted
        card lists empty, so nothing can be deleted from it.
        """
        payload = get_json(
            self._url({"op": "list", "path": rel_dir}), timeout_s=self.timeout_s
        )
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise self._error(
                f"{FILES_PATH} list from {self.host}: malformed reply {payload!r}"
            )
        return {
            str(e["name"]): e for e in entries if isinstance(e, dict) and e.get("name")
        }

    def delete_file(self, rel_dir: str, name: str) -> None:
        """Delete ONE file ``<rel_dir>/<name>``.

        Raises on any refusal (``409`` for a file being recorded or while an
        ECU flash runs, ``403`` reserved, ``404`` gone) or an unexpected reply.
        """
        name = sanitize_basename(name)
        if not DELETABLE_NAME.fullmatch(name):
            raise self._error(f"refusing to delete {name!r}: unsupported characters")
        reply = post_json(
            self._url(),
            {"op": "delete", "path": f"{rel_dir}/{name}"},
            timeout_s=self.timeout_s,
        )
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise self._error(f"delete of {name} refused: {reply!r}")
        if reply.get("deleted") != 1:
            # The listing said it was one plain file; anything else means the
            # device changed under us — make it loud.
            logger.warning(
                "Delete of %s/%s removed %r entries",
                rel_dir,
                name,
                reply.get("deleted"),
            )
