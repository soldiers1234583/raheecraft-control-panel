# 🎮 Kaggle API — Minecraft Server Project

This project uses the **Kaggle API** to manage your Minecraft server notebook.

---

## 🔑 Your Kaggle Credentials

```
Username: riyanahasanaraf
API Key: aa5c40591d6312c2124628fd13e519c3
Access Token: KGAT_533e3a3edd4ecdb4366439b479b8d374
```

> ⚠️ **Security Note:** Never share these credentials publicly. They're stored in `~/.kaggle/kaggle.json` and `~/.kaggle/access_token`.

---

## 📦 Files

| File | Purpose |
|------|---------|
| `minecraft_kaggle_setup.ipynb` | The main Minecraft server notebook (8 cells) |
| `kaggle_api_setup.py` | Python script to upload/manage notebooks via API |
| `KAGGLE_MINECRAFT_GUIDE.md` | User guide for running the server |

---

## 🚀 Quick Start

### Option 1: Via Kaggle CLI (automated)

```bash
# Run the API setup script
python3 kaggle_api_setup.py
```

This will:
1. Verify your Kaggle API credentials
2. Upload the notebook to your Kaggle account
3. List your existing notebooks
4. Show session info

### Option 2: Manual Upload

1. Go to https://www.kaggle.com/code
2. Click "New Notebook"
3. Copy cells from `minecraft_kaggle_setup.ipynb`
4. Run cells top-to-bottom

### Option 3: Clone via Git (if you push this repo)

```bash
# If you push this to GitHub
git clone https://github.com/your-repo/minecraft-kaggle.git
cd minecraft-kaggle
python3 kaggle_api_setup.py
```

---

## 📋 Using the Notebook

1. **Open** the notebook on Kaggle
2. **Run Cell 1** — Environment check (confirms Java, RAM, internet)
3. **Edit Cell 2** — Configure settings (edition, players, seed)
4. **Run Cell 3** — Download Paper server
5. **Run Cell 4** — Generate config files
6. **Run Cell 5** — Start the server
7. **Run Cell 6** — Set up ngrok tunnel
8. **Run Cell 7** — View connection info
9. **Run Cell 8** — Backup world before closing

---

## 🔧 Kaggle API Commands

### Authenticate
```bash
kaggle auth login
```

### List Notebooks
```bash
kaggle kernels list --me
```

### Create Notebook
```bash
kaggle kernels create -p . -m "# My Notebook"
```

### Push Updates
```bash
kaggle kernels push -p .
```

### Download Dataset
```bash
kaggle datasets download -d username/dataset-name
```

---

## ⚠️ Important Notes

### Kaggle Session Limits
- **9-hour max** per session
- World data **lost** when session ends
- **No inbound connections** — must use ngrok

### ngrok Setup (Required for Friends to Connect)
1. Sign up at https://ngrok.com
2. Get auth token from dashboard
3. Paste token in Notebook Cell 6
4. Run Cell 6 to create tunnel

### World Backup
Before closing the notebook:
```python
!zip -r world_backup.zip /kaggle/working/minecraft_server/world/
```

Then download from Kaggle Files panel.

---

## 🛠️ Troubleshooting

| Issue | Solution |
|-------|----------|
| API auth failed | Run `kaggle auth login` |
| Notebook upload failed | Use manual upload via web UI |
| Java not found | Run `!apt-get install -y default-jdk` in notebook |
| Out of memory | Reduce `JAVA_XMX` in Cell 2 |
| Can't connect from outside | Verify ngrok tunnel is running |

---

## 📚 Resources

- [Kaggle API Docs](https://www.kaggle.com/docs/api)
- [Kaggle CLI GitHub](https://github.com/Kaggle/kaggle-cli)
- [PaperMC Downloads](https://papermc.io/downloads)
- [ngrok](https://ngrok.com)
- [Minecraft EULA](https://minecraft.net/eula)
