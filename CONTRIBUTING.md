# How we work together on this code (team guide)

Read this once before you start. It keeps us from deleting each other's work.

## The 4 golden rules

1. **Never work directly on `main`.** `main` is the working version everyone trusts. Make your own branch.
2. **Always pull before you start working.** Get your friends' latest changes first.
3. **Never commit `.env`.** It has our API keys. If keys reach GitHub, anyone can spend our money. (`.gitignore` blocks it — don't force it.)
4. **Small changes, clear messages.** One branch = one fix or feature. Message says what changed: `Fix English not switching`, not `update`.

## Words you need to know

| Word | Meaning |
|---|---|
| **repo** | The project folder on GitHub. |
| **clone** | Download the repo to your PC (done once). |
| **branch** | Your own copy of the code to work on, without touching `main`. |
| **commit** | Save a snapshot of your changes, with a message. |
| **push** | Upload your commits to GitHub. |
| **pull** | Download the latest changes from GitHub to your PC. |
| **Pull Request (PR)** | A request on GitHub: "please add my branch into `main`". A friend checks it first. |
| **merge** | Join a branch into `main`. |
| **conflict** | Two people changed the same lines. Git asks you to choose which to keep. |

## First time only — set up on your PC

Install **Git** (git-scm.com) and **Python 3.11**. Then in PowerShell:

```powershell
# 1. Tell git who you are (use your GitHub name and email)
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# 2. Download the project
cd Desktop
git clone https://github.com/sk-inthiyaz/RanaAI_voiceAgent.git
cd RanaAI_voiceAgent

# 3. Python setup
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 4. Keys: copy the example and ask the team lead for the real values (send privately, never in the group chat)
copy .env.example .env
```

## Every time you work — the daily flow

```powershell
# STEP 1: go to main and get the latest code
git checkout main
git pull

# STEP 2: make your own branch (name = your-name/what-you-are-doing)
git checkout -b ravi/fix-voice-lag

# STEP 3: edit the code ... test it with a real call ...

# STEP 4: see what you changed
git status
git diff

# STEP 5: save (commit) your changes
git add bot.py agent_prompt.txt          # add the files you changed (avoid "git add ." if unsure)
git commit -m "Reduce reply lag: silence wait 0.7s"

# STEP 6: upload your branch
git push -u origin ravi/fix-voice-lag
```

**STEP 7 — open a Pull Request:** go to the repo on GitHub → yellow bar "Compare & pull request" → write what you changed and how you tested it → **Create pull request**. Tell the team in the group. Someone else reviews and clicks **Merge**.

**STEP 8 — after it is merged:**
```powershell
git checkout main
git pull
git branch -d ravi/fix-voice-lag     # delete your old branch
```
Then start again from STEP 2 for the next task.

## While your branch is open, main changed — get the latest

```powershell
git checkout main
git pull
git checkout ravi/fix-voice-lag
git merge main
```

## If you get a CONFLICT

Git shows the file with markers like this:
```
<<<<<<< HEAD
your version
=======
your friend's version
>>>>>>> main
```
1. Open the file in VS Code — it shows buttons **Accept Current / Accept Incoming / Accept Both**. Pick the right one.
2. Make sure no `<<<<<<<`, `=======`, `>>>>>>>` lines are left.
3. Then:
```powershell
git add <the-file>
git commit -m "Merge main into my branch"
git push
```
Not sure? **Stop and ask** in the group before pushing. Never delete a friend's code to "make it work".

## Who works on what (avoid conflicts)

Before starting, post in the group: *"I'm working on bot.py — voice lag."* Two people editing the same file at the same time = conflicts.

| File | What it is |
|---|---|
| `bot.py` | The agent: speech-to-text, LLM, voice, language switching |
| `phone_server.py` | Phone calls through Vobiz |
| `agent_prompt.txt` | What the agent says and how it behaves (RANA sales) |
| `business_receptionist_prompt.txt` | Receptionist prompt for business customers |
| `gemini_business_extractor_prompt.txt` | Prompt to get a business's data from its website |
| `.env.example` | List of settings — update it when you add a new setting (with an empty value) |

## Running the agent

See `README.md`. After **any** change to code or `.env`, stop the server (`Ctrl+C`) and start it again.

## Emergency — "I committed .env by mistake"

Tell the team lead **immediately**. Don't just delete the file — the keys stay in GitHub history. The keys must be changed (rotated) in the Sarvam and Vobiz dashboards.
