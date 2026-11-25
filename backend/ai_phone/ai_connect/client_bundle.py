"""Ship the same pure-Python WebSocket client used by the host, including its license."""
from functools import lru_cache
from importlib.metadata import distribution
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


@lru_cache(maxsize=1)
def client_bundle():
    library = distribution("websockets")
    result = BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(Path(__file__).with_name("connect_client.py"), "__main__.py")
        archive.write(Path(__file__).with_name("hdc_ports.py"), "hdc_ports.py")
        for entry in library.files or ():
            if entry.parts[0] == "websockets" and entry.suffix == ".py":
                archive.write(library.locate_file(entry), str(entry))
            elif entry.name == "LICENSE" and ".dist-info" in entry.parts[0]:
                archive.write(library.locate_file(entry), "websockets-LICENSE")
    return result.getvalue()
