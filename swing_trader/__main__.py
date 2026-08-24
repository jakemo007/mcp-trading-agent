import argparse
import logging
import sys

# Force UTF-8 output so ₹ renders correctly on all platforms
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from swing_trader.config import CAPITAL_INITIAL, MAX_POSITIONS, MIN_RR, WATCHLIST

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing Trader — local runner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and log signals without writing any positions",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  SWING TRADER  —  Nifty 100 Watchlist")
    print("=" * 60)
    print(f"  Capital        : ₹{CAPITAL_INITIAL:,.0f}")
    print(f"  Max per trade  : 25%  (₹{CAPITAL_INITIAL * 0.25:,.0f})")
    print(f"  Max risk       :  2%  (₹{CAPITAL_INITIAL * 0.02:,.0f})")
    print(f"  Min RR         :  {MIN_RR}:1")
    print(f"  Max positions  :  {MAX_POSITIONS}")
    print(f"  Watchlist      :  {len(WATCHLIST)} tickers")
    print(f"  Entry window   :  09:30 – 14:30 IST (Mon–Fri)")
    if args.dry_run:
        print("  Mode           :  DRY RUN (no positions will be written)")
    print("=" * 60)
    print("  Press Ctrl+C to stop\n")

    if args.dry_run:
        from swing_trader.runner import run_daily_scan
        run_daily_scan(dry_run=True)
        sys.exit(0)

    try:
        from swing_trader.runner import main_loop
        main_loop()
    except KeyboardInterrupt:
        print("\n[STOP] Swing trader stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
