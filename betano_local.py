#!/usr/bin/env python
"""Betano's tennis ace and double-fault odds, collected from this PC.

Betano refuses the posession server (HTTP 403 since about 7 Nov 2026), so
its run_aces.sh still collects results and settles, but the odds that
betken.cz's Tennis page shows are fetched here: store every rung, price,
write tips (odds.py --advise). Telegram is not sent from here.

The Windows scheduled task "aces Betano odds" runs this every three hours
with .venv\\Scripts\\pythonw.exe, which opens no window, at normal priority
(4): at the scheduler's default (7, below normal) its runs sat at process
start for hours, from 19 Nov 2026 21:50 on, and blocked every later one. A
new run stops one still going. Output goes to betano_local.log. By hand:

    .venv\\Scripts\\python.exe betano_local.py
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

with open(HERE / "betano_local.log", "a", encoding="utf-8", buffering=1) as log:
    sys.stdout = sys.stderr = log          # pythonw has no console to write to
    stamp = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"== {stamp()} aces Betano odds")
    # A run takes about a minute. One that stalls writes where it stuck and
    # exits, rather than holding the task (one instance at a time) for ever.
    import faulthandler
    faulthandler.dump_traceback_later(int(os.environ.get("ACES_WATCHDOG", 900)), exit=True, file=log)
    # On 20 Nov 2026 12:40 a run exited with 1 after ten seconds and logged
    # nothing: whatever escaped went to a stderr already closed. So catch
    # everything, BaseException included, and try once more after a minute.
    import time
    import traceback
    code = 1
    for attempt in (1, 2):
        try:
            from dotenv import load_dotenv
            import odds
            logging.basicConfig(level=logging.INFO, stream=log,
                                format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
            load_dotenv(HERE / ".env")
            sys.argv = ["odds.py", "--advise"]
            code = odds.main() or 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except BaseException as e:
            code = 1
            print(f"== {stamp()} attempt {attempt} failed: {type(e).__name__}: {e}")
            traceback.print_exc()
        if code == 0 or attempt == 2:
            break
        print(f"== {stamp()} exit {code}, retrying in 60 s")
        time.sleep(60)
    print(f"== {stamp()} exit {code}")
sys.exit(code)
