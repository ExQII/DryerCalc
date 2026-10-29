import sys


def _main() -> None:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.txt"
        from app.selftest import run
        sys.exit(run(out))
    from app.gui import main
    main()


if __name__ == "__main__":
    _main()
