# Lecture Tool — install on your Mac

Turns a lecture PDF (script, slides, exercise sheet) into a compact lecture, a summary and a
narrated video, on your own Mac, with your own Claude subscription (Pro or Max).

Open **Terminal** (Spotlight → Terminal), paste this one line exactly as it is and press Enter:

```
curl -fsSL https://raw.githubusercontent.com/fpv-mp4/lecture-tool/main/get.sh -o /tmp/lt-get.sh && bash /tmp/lt-get.sh
```

What happens: if Homebrew is not installed yet it is installed first (asks for your Mac password once;
on a fresh Mac this may also open a window to install Apple's command line tools — when that is done,
run the same line again). Then the tool is fetched into `~/lecture-tool`, ffmpeg, Python and Claude Code
are installed, you sign in to Claude in your browser, and the panel opens at http://localhost:8765.
The panel starts by itself every time you log in; from then on you just open that address.

Optional: for the better narration voice, paste an ElevenLabs API key under Settings → Narration.
Without it the Mac voice is used.

If something goes wrong, these two fixes cover it (paste them exactly; none of them contains quotes):

- Sign-in error instead of a browser window: open a new Terminal window, run `claude auth login`,
  sign in there, then paste the install line once more.
- "Permission denied" for Library/LaunchAgents: run

```
sudo chown -R $USER ~/Library/LaunchAgents
```

  (asks for your Mac password, nothing is shown while typing), then paste the install line again.

If the Terminal ever shows `dquote>` or `quote>`, press Control+C and paste the command again from this file.

Updates: Settings → "Check for updates". When a new version is out, one click installs it.
