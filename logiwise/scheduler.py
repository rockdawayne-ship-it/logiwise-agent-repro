"""주기 실행 CLI (STEP0_PLAN 1.8).

    python -m logiwise.scheduler [--interval SEC] [--once] [--rules-only]

- 시작 시 ``init_schema`` + ``seed``(데이터가 없을 때만)
- ``--interval`` 기본값은 ``config.toml [monitor].interval_seconds``. 최소 30초(작으면 30으로 올림)
- ``--once``: 한 사이클만 돌고 종료
- ``--rules-only``: 실제 Claude 를 호출하지 않고 ``rules.rule_report`` 로 대체(비용 없음)
- 사이클마다 한 줄 로그: ``run_id status mode detected/targets/skipped drafts``
- ``Ctrl+C``(KeyboardInterrupt) 로 종료
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import db, monitor, seed, settings

log = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 30


def resolve_interval(value: float | int | None) -> int:
    """간격(초)을 정수로 정리하고 최소 30초를 보장한다. None 이면 config 값."""
    if value is None:
        value = settings.MONITOR.get("interval_seconds", 300)
    try:
        sec = int(float(value))
    except (TypeError, ValueError):
        sec = MIN_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, sec)


def format_cycle(result: monitor.CycleResult) -> str:
    """한 줄 로그. ``[cycle] run_id=… status=… mode=… detected=… targets=… skipped=… drafts=…``"""
    return "[cycle] " + result.summary_line()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LOGIWISE 자율 모니터링 스케줄러")
    parser.add_argument("--interval", type=float, default=None,
                        help=f"사이클 간격(초). 기본 config.toml [monitor].interval_seconds, 최소 {MIN_INTERVAL_SECONDS}")
    parser.add_argument("--once", action="store_true", help="한 사이클만 실행하고 종료")
    parser.add_argument("--rules-only", action="store_true",
                        help="Claude 를 호출하지 않고 규칙 기반 대체 경로만 사용(비용 없음)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    interval = resolve_interval(args.interval)
    # config.toml [monitor].use_agent = false 면 CLI 플래그 없이도 규칙 경로만 쓴다. --rules-only 는 항상 우선.
    use_agent = bool(settings.MONITOR.get("use_agent", True)) and not args.rules_only

    db.init_schema()
    if seed.seed():
        print("[seed] 샘플 데이터 생성")
    print(f"[scheduler] db={settings.get_db_path()} interval={interval}s mode={'agent' if use_agent else 'rules'}"
          f"{' once' if args.once else ''}", flush=True)

    exit_code = 0
    try:
        while True:
            result = monitor.run_cycle(trigger="scheduler", use_agent=use_agent)
            print(format_cycle(result), flush=True)
            if result.status == "failed":
                exit_code = 1
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[scheduler] 종료(KeyboardInterrupt)", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
