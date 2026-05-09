try:
    from .main import main
except ImportError:
    from coverup.main import main

if __name__ == "__main__":
    raise SystemExit(main())
