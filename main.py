import sys


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_yt-dlp":
        import yt_dlp
        yt_dlp.main(sys.argv[2:])
    else:
        from app.cli import run
        run()
