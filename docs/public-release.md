# Publish from the development repository

Keep private development history and public release history separate.

1. Copy the files you intend to release into a clean public checkout. Exclude
   `AGENTS.md`, `CLAUDE.md`, `.agents/`, `.audit/`, `docs/adr/`, `.agents/adr/`,
   and `docs/naru-persistent-state-report.html`.
2. Review `git diff` and `git status --short`. Do not merge or push a private
   development branch into this repository. Ignore rules do not remove files
   that Git already tracks.
3. Follow [the contribution checks](../CONTRIBUTING.md). Check local links in
   the README and guides. Keep user documentation with the code it describes.
4. Commit the reviewed public changes with a short Conventional Commit subject.
   Check `git log -1 --format=full` for private notes and session identifiers
   before you push. Follow any commit attribution policy that applies to your
   environment.
5. Push the public branch and check GitHub Actions.

The public-file check rejects known private paths. It cannot decide whether a
new document contains private information. Review every new file before release.
