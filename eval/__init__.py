# The scorer reuses the API's citation parser (app.agent.citations), so make `app` importable.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
