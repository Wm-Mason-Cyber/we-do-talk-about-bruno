# We Do Talk About Bruno 🐶

The classroom server for the **Exploring APIs with Bruno** lab (AP Cybersecurity).
Students use [Bruno](https://www.usebruno.com) to `GET`, `POST`, `PUT`, and `DELETE`
themselves on a class roster. A projector dashboard shows every request, and the
status code it got back, within about a second.

It runs on anything Debian-flavored: a **Raspberry Pi**, a **Proxmox LXC**, a VM, or a laptop.

Everything is on one port (5000):

| URL | For |
|---|---|
| `http://<host>.local:5000/students` | Students (Bruno) |
| `http://<host>.local:5000/dashboard` | **Teacher, on the projector.** Live roster and request feed. |
| `http://<host>.local:5000/docs` | Auto-generated interactive API docs |
| `http://<host>.local:5000/logs/access` | Every request, in the standard "combined" log format (FRQ-style log reading) |

`<host>` is the machine's hostname, e.g. `b112-rpi` on the classroom Pi.

---

## Quick start

```bash
sudo apt install -y git          # if it isn't there already
git clone https://github.com/Wm-Mason-Cyber/we-do-talk-about-bruno.git
cd we-do-talk-about-bruno
./setup.sh
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 5000
```

That's it. Open `http://<host>.local:5000/dashboard` on the projector. The server
prints the **teacher key** when it starts. You need it for the dashboard's *Teacher*
button, and it's also kept in `.teacher_key`.

`setup.sh` installs avahi (for `.local` names) and the Python packages into `.venv/`.
If pip can't install them (this happens on 32-bit Raspberry Pi OS), it falls back to the
system's `python3-fastapi` and `uvicorn` packages automatically. Stop the server with `Ctrl`+`C`.

### Keep it running (start at boot)

```bash
./install-service.sh
```

This creates the `bruno-api` systemd service on port 5000. It restarts if it crashes and
starts at every boot, so nobody has to SSH in before class. To remove it later, run
`./install-service.sh --remove`.

### Updating

```bash
cd we-do-talk-about-bruno
git pull && ./setup.sh && ./install-service.sh
```

---

## On the classroom Raspberry Pi (replacing the old server)

The old server runs as `student-api.service` on port 5000. Test the new one next to it
on port **5001** first:

```bash
git clone https://github.com/Wm-Mason-Cyber/we-do-talk-about-bruno.git
cd we-do-talk-about-bruno
./setup.sh
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 5001
```

Check `http://b112-rpi.local:5001/dashboard` from the projector computer, and send a
request or two from Bruno. When you're happy, press `Ctrl`+`C`, then swap the new
server in on port 5000:

```bash
sudo systemctl disable --now student-api     # retire the old server
./install-service.sh                         # new one on :5000, starts at boot
```

Student instructions that say `http://b112-rpi.local:5000` keep working unchanged.
To roll back: `./install-service.sh --remove && sudo systemctl enable --now student-api`.

---

## On a Proxmox LXC

1. **Create a Debian 12 container** (web UI → *Create CT*): template `debian-12-standard`,
   1 core, 512 MB RAM, 4 GB disk, *Unprivileged* ✔, *Nesting* ✔. Afterward, tick
   *Start at boot* under Options.
2. **Network: use a static IP that nothing else uses** (*IPv4: Static*, with your gateway).
   With DHCP, the container can end up sharing an address with another server. Students'
   requests then land on that server instead (this has happened here, with a GitLab VM).
   Before you pick, check on the Proxmox host that nothing answers at the address:
   `ping -c2 <IP>; ip neigh show <IP>` should show no MAC address.
3. **Put it on a network the classroom laptops can reach.** `.local` names only work when
   the container is on the same subnet/VLAN as the laptops. Otherwise students use the IP.
4. **Install** from the Proxmox host shell:

```bash
pct enter <VMID>
apt update && apt install -y git
git clone https://github.com/Wm-Mason-Cyber/we-do-talk-about-bruno.git /opt/we-do-talk-about-bruno
cd /opt/we-do-talk-about-bruno
./setup.sh
./install-service.sh
```

The last command prints the student URL, the projector URL, and the teacher key.

---

## Day to day

| Task | How |
|---|---|
| Start of class | Open `http://<host>.local:5000/dashboard` on the projector. That's it. |
| Between periods | Dashboard → **Teacher** → enter key → **New period (reset)** |
| Remove an inappropriate name | Teacher mode → click **×** on the card. It sends a normal `DELETE /students/{id}`, so it shows in the feed too. |
| Full reset from the shell | `sudo systemctl restart bruno-api` (the roster lives in memory) |
| Bigger text on the projector | `Ctrl` + `+` in the browser. The layout reflows. |
| Light theme (washed-out projector) | The ◐ button in the top right |
| See the teacher key | `cat .teacher_key` in the repo folder |
| Server logs | `journalctl -u bruno-api -f` |

Students can open `/dashboard` too. It's read-only for them: reset needs the teacher
key, and the × buttons only do what any student can already do with `DELETE`.

### Built-in guard rails

- Names must be 1–40 characters, or the API returns `422 Unprocessable Content`
  (a status code students can discover on purpose).
- The roster is capped at 200 students. After that it returns `507 Insufficient Storage`,
  so a runaway Bruno Runner loop can't fill memory.
- The dashboard inserts names as plain text, so `<script>` names show up as text and
  never run on the projector.
- `logs/access.log` rolls over to `access.log.1` at 5 MB.

---

## Troubleshooting

**`<host>.local` doesn't resolve, but the IP works.**
mDNS only works within one subnet/VLAN, and some networks block it. Use
`http://<IP>:5000` in the student instructions. Check that avahi is running with
`systemctl status avahi-daemon`. WSL can't resolve `.local` names, so test from a real
laptop.

**The address opens some *other* server (a login page, another app).**
Two machines are using the same IP. Give this one an address nobody else uses (see the
LXC section above).

**`install-service.sh` says something is already listening on port 5000.**
On the Pi that's the old server: `sudo systemctl disable --now student-api`.
Otherwise, `PORT=5001 ./install-service.sh`.

**The service won't start.**
`journalctl -u bruno-api -n 50` shows why. Running the uvicorn command by hand in the
repo folder shows the same errors in your terminal.

**Dashboard says "reconnecting…" over and over.**
Usually a proxy or content filter between the projector computer and the server that
buffers long-lived connections. The page reconnects on its own. Try the IP directly to
bypass a filtering proxy.

---

## The API

| Method | Path | Does | Typical codes |
|---|---|---|---|
| `GET` | `/students` | List the roster | 200 |
| `GET` | `/students/{id}` | One student | 200, 404 |
| `POST` | `/students` | Add `{"name": "..."}` | 201, 422, 507 |
| `PUT` | `/students/{id}` | Rename `{"name": "..."}` | 200, 404, 422 |
| `DELETE` | `/students/{id}` | Remove | 200, 404 |
| `GET` | `/admin-only` | Secret data. Needs header `X-API-Key: bruno-is-awesome` | 200, 403 |

### What's in the repo

| Path | What |
|---|---|
| `roster_api.py` | The API itself, written to be read by students. Runs on its own: `uvicorn roster_api:app --reload` |
| `dashboard.py` | Request-watching middleware, the live projector page (Server-Sent Events), and the access log |
| `app.py` | What you run: the API with the dashboard attached, plus the teacher key |
| `setup.sh` | Installs dependencies into `.venv/` (falls back to apt packages on older Pis) |
| `install-service.sh` | Optional: run at boot with systemd |
| `test_server.py` | Tests; GitHub Actions runs them on every push, against both pinned and Debian 12 package versions |

### Running the tests

```bash
./setup.sh
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest test_server.py -v
```

Licensed under GPL-3.0. See [LICENSE](LICENSE).
