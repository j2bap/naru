# Prepare a public release

[README](../README.md) · [Contributing](../CONTRIBUTING.md)

Keep public release history separate from private development history. Use a
clean public checkout and copy only the files you intend to release.

1. **Select the files.** Exclude private agent instructions, design records,
   audit output, and local reports. The
   [public-file CI check](../.github/workflows/checks.yml) lists the paths it
   rejects.
2. **Review the content.** Run `git status --short` and `git diff`. Inspect
   each new file as well as edits to tracked files. Ignore rules do not remove
   files that Git already tracks.
3. **Verify the release.** Run the
   [offline contribution checks](../CONTRIBUTING.md#run-the-offline-checks).
   Check links in the README and guides. Run changed examples in a temporary
   store and keep docs with the code they describe.
4. **Review the commit.** Use a short Conventional Commit subject. Run
   `git log -1 --format=full` and check that the message contains no private
   notes or session identifiers. Follow the commit attribution policy that
   applies to your environment.
5. **Publish and check CI.** Push the reviewed public branch and inspect
   GitHub Actions. Keep private branches and their history out of the public
   remote.

The public-file check catches known paths. You still need to review file
contents for private information before release.
