# 🎮 Minecraft PvP Name Scanner

A Python tool designed to scan short Minecraft usernames (4–5 characters) using the Mojang API.

Find new username possibilities, check their availability status, and save your scan history automatically.

## ✨ Features

- Scan up to **10,000 names per scan**
- Generate new 4–5 character Minecraft usernames
- Start unlimited new scans
- Avoid checking previously scanned names
- Parallel username checking
- Discord webhook notifications
- Dark-themed graphical interface
- Live scan progress and statistics
- Save scan history locally
- Retry handling for API limits and temporary errors
- API errors are never incorrectly marked as available

## 🖥️ Requirements

- Windows, Linux or macOS
- Python 3.10 or newer
- Internet connection

**Recommended:** Python 3.12 (64-bit)

No additional Python packages are required.

## 🚀 Installation

1. Install Python 3.10 or newer.
2. Download the Python file from this repository.
3. Open the folder containing the file.
4. Run the following command:

```bash
python minecraft_pvp_name_scanner.py
```

## 🔄 How it works

1. Open the scanner.
2. Choose your scan settings.
3. Start scanning.
4. View the results in real time.
5. Start another scan whenever you want.

Previously checked names are stored locally, so future scans can focus on new generated names.

## ⚠️ Important Information

The scanner uses the Mojang API to check usernames.

An API response indicating that no profile was found does **not guarantee** that a username can be claimed. Minecraft username availability can depend on additional restrictions and account rules.

Please use the scanner responsibly and respect API rate limits.

## ❤️ Thank You!

Thank you so much for using this project!

I appreciate every person who downloads, tests, and supports the scanner. This project was created to make searching for short Minecraft PvP usernames easier.

If you like this project, consider giving the repository a ⭐ Star on GitHub.

**Enjoy scanning and good luck finding your perfect username!**

---

Made with ❤️ and Python.