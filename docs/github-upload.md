# First GitHub upload

Repository: `https://github.com/dozyanka/pm-assistant`

Extract this source package directly into:

```text
C:\Users\shata\Desktop\GitHub\pm-assistant
```

Open PowerShell in that folder and run:

```powershell
git init
git branch -M main
git config --global user.name "DVSh (Dozya)"
git add -A
git status
git commit -m "feat: initial public release"
git remote add origin https://github.com/dozyanka/pm-assistant.git
git push -u origin main
```

Before `git commit`, review `git status` and make sure model weights, databases, virtual environments and build outputs are not staged. The included `.gitignore` excludes them.

If GitHub asks you to authenticate, complete the browser/Git Credential Manager login flow. Do not use your GitHub password as a Git password.

If the GitHub repository is not empty because you created a README/license on github.com, do not use `--force`. Clone the repository first or reconcile the existing commit before pushing.
