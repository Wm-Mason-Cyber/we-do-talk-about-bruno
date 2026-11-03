# We Do Talk About Bruno 🐶

The classroom server for the **Exploring APIs with Bruno** lab (AP Cybersecurity).
Students use [Bruno](https://www.usebruno.com) to `GET`, `POST`, `PUT`, and `DELETE`
themselves on a class roster. A projector dashboard shows every request, and the
status code it got back, within about a second.

It runs as a small, always-on Debian LXC on Proxmox. The container pulls its code
from this repo. It starts at boot, restarts itself if it crashes, and resets the
roster at 05:00 every day.

| URL | For |
|---|---|
| `http://bruno-api.local:5000/students` | Students (Bruno) |
| `http://bruno-api.local/dashboard` | **Teacher, on the projector.** Live roster and request feed. |
| `http://bruno-api.local:5000/docs` | Auto-generated interactive API docs |
| `http://bruno-api.local:5000/logs/access` | Raw nginx access log (FRQ-style log reading) |

### The API

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
| `roster_api.py` | The API itself, written to be read by students. Runs on its own: `uvicorn roster_api:app --reload --port 5000` |
| `dashboard.py` | Request-watching middleware and the live projector page (Server-Sent Events) |
| `app.py` | Production entry point: the API with the dashboard attached |
| `test_server.py` | Tests; GitHub Actions runs them on every push |
| `deploy/install.sh` | Installs or updates everything inside the container |
| `deploy/*` | nginx site, systemd units (service and the 05:00 reset timer), logrotate |

Inside the container:

| Piece | Job |
|---|---|
| `bruno-api.service` | uvicorn running `app:app` on `127.0.0.1:8000`, from `/opt/bruno-api/src` (a clone of this repo) |
| nginx | Listens on **:5000** and **:80**. Writes a standard combined-format access log to `/var/log/bruno-api/`. |
| avahi | Advertises `<hostname>.local` so laptops on the same network find it by name |
| `bruno-api-nightly.timer` | Restarts the API at 05:00, so each day starts with only Ada and Grace |

---

## Setting up the LXC

### 0. Decide one thing first: can student laptops reach the container?

This is the only part that can really go wrong. The container has to be on a network
the classroom laptops can reach.

- **Best:** the same VLAN/subnet as the classroom laptops. Then `bruno-api.local`
  resolves through mDNS with no setup.
- **OK:** a different subnet that's routed to the classroom. mDNS will **not** cross
  subnets, so students use the IP address (`http://10.x.x.x:5000`), or IT adds a DNS
  record. Give the container a **DHCP reservation or a static IP** so the address
  doesn't change.
- **Won't work:** a management/server VLAN that classroom devices can't route to.

If you're not sure, try it first: from a student laptop, `ping` some other machine on
the Proxmox bridge/VLAN you plan to use.

### 1. On the Proxmox host: create the container

Open a shell on any node (web UI → node → **>_ Shell**, or `ssh root@<node>`).
Adjust the variables at the top, then paste the whole block.

```bash
# ── adjust these ──────────────────────────────────────────────
VMID=120                 # any free ID:  pvesh get /cluster/nextid
STORAGE=local-lvm        # where the root disk lives (local-lvm, ceph pool, etc.)
BRIDGE=vmbr0             # the bridge that reaches the classroom network
VLAN=                    # e.g. 112 if the classroom is a tagged VLAN; blank if not
HOSTNAME=bruno-api       # becomes  http://bruno-api.local
# ──────────────────────────────────────────────────────────────

pveam update
TEMPLATE=$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)
echo "Using template: $TEMPLATE"
pveam download local "$TEMPLATE"

pct create "$VMID" "local:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores 1 --memory 512 --swap 512 \
  --rootfs "$STORAGE:4" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp${VLAN:+,tag=$VLAN}" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 \
  --description "Bruno lab: Class Roster API + live dashboard — github.com/Wm-Mason-Cyber/we-do-talk-about-bruno"

pct start "$VMID"
sleep 5
pct exec "$VMID" -- ip -4 -brief addr show eth0
```

The last line prints the container's IP. **Write it down.** If you can, set a DHCP
reservation for it now (`pct config $VMID | grep hwaddr` shows the MAC).

Notes:

- **Debian 12** is used on purpose. It works on both Proxmox VE 8 and 9. The app also
  runs on Debian 13 (CI tests both Python versions).
- `nesting=1` is what the Proxmox GUI sets by default for unprivileged containers.
  Recent Debian's systemd needs it.
- 1 core, 512 MB RAM, and a 4 GB disk is plenty. The app idles at about 60 MB.
- **Prefer the GUI?** *Create CT* with the same settings: hostname `bruno-api`, Debian 12
  template, 4 GB disk, 1 core, 512 MB, DHCP on the classroom bridge/VLAN, and tick
  *Start at boot* under Options afterward.

### 2. Still on the Proxmox host: install from GitHub

```bash
pct exec "$VMID" -- env LC_ALL=C.UTF-8 bash -c 'apt-get update -qq && apt-get install -y -qq curl >/dev/null && \
  curl -fsSL https://raw.githubusercontent.com/Wm-Mason-Cyber/we-do-talk-about-bruno/main/deploy/install.sh | bash'
```

The installer clones this repo to `/opt/bruno-api/src`, sets everything up, and finishes
by printing the URLs and your **teacher key**. You need the key for the dashboard's
*Teacher* button. To see it again later:

```bash
pct exec "$VMID" -- cat /etc/bruno-api.env
```

The container needs outbound internet to reach GitHub, apt, and PyPI when installing
or updating. While it's serving the class, it doesn't.

### 3. Check it from a student laptop

1. Browser: `http://bruno-api.local:5000/students` → JSON with Ada and Grace.
   If the name doesn't resolve, try `http://<IP>:5000/students`. If the IP works but the
   name doesn't, see Troubleshooting.
2. Browser on the projector computer: `http://bruno-api.local/dashboard` → the green
   dot reads **live**.
3. Bruno on a laptop: `POST /students` with `{"name": "test"}`. It shows up on the
   dashboard within about a second.

### Optional: high availability across the cluster

`--onboot 1` already restarts it whenever its node reboots. If the root disk is on
**shared storage** (Ceph, NFS, etc.), you can also let the cluster move it if a node dies:

```bash
ha-manager add ct:$VMID --state started
```

Skip this if `$STORAGE` is local to one node. HA can't move a local disk.

---

## Day to day

| Task | How |
|---|---|
| Start of class | Open `http://bruno-api.local/dashboard` on the projector. That's it. |
| Between periods | Dashboard → **Teacher** → enter key → **New period (reset)** |
| Remove an inappropriate name | Teacher mode → click **×** on the card. It sends a normal `DELETE /students/{id}`, so it shows in the feed too. |
| Bigger text on the projector | `Ctrl` + `+` in the browser. The layout reflows. |
| Light theme (washed-out projector) | The ◐ button in the top right |

Students can open `/dashboard` too. It's read-only for them: reset needs the teacher
key, and the × buttons only do what any student can already do with `DELETE`.

### Built-in guard rails

- Names must be 1–40 characters, or the API returns `422 Unprocessable Content`
  (a status code students can discover on purpose).
- The roster is capped at 200 students. After that it returns `507 Insufficient Storage`,
  so a runaway Bruno Runner loop can't fill memory.
- nginx rejects request bodies over 4 KB.
- The dashboard inserts names as plain text, so `<script>` names show up as text and
  never run on the projector.

### Updating

Push to `main` (wait for the green check in GitHub Actions), then on the Proxmox host:

```bash
pct exec "$VMID" -- bash /opt/bruno-api/src/deploy/install.sh
```

The installer is safe to repeat: it pulls the latest commit, keeps the teacher key,
and restarts the service. To deploy a different branch, run it with `BRANCH=<name>`
in front.

### Maintenance

`pct enter $VMID` on the Proxmox host gives you a root shell in the container. Then:

```bash
systemctl status bruno-api                       # is it running?
journalctl -u bruno-api -f                       # app logs (errors, restarts)
tail -f /var/log/bruno-api/access.log            # every request, nginx combined format
systemctl restart bruno-api                      # manual full reset
systemctl list-timers bruno-api-nightly.timer    # when's the next 05:00 reset?
git -C /opt/bruno-api/src log -1 --oneline       # which commit is deployed?
```

**Backups:** there's no persistent data, so there's nothing to back up. Rebuilding is
steps 1–2.

---

## Troubleshooting

**`bruno-api.local` doesn't resolve, but the IP works.**
mDNS only works within one subnet/VLAN. Either the container is on a different subnet
from the laptops (see step 0), or the network blocks multicast. Use the IP in the
student instructions, or ask IT for a DNS A record. Check that avahi is running with
`systemctl status avahi-daemon` inside the container.

**Nothing answers, not even the IP.**
Inside the container, run `curl localhost:5000/students`.
- If that works, it's the network path: wrong bridge/VLAN, or the Proxmox firewall on
  the CT (*CT → Firewall* must allow TCP 80 and 5000 inbound, if the firewall is enabled).
- If it fails, check `systemctl status bruno-api nginx` and `journalctl -u bruno-api -n 50`.

**Dashboard says "reconnecting…" over and over.**
Usually a proxy or content filter between the projector computer and the container
that buffers long-lived connections. The page reconnects on its own and catches up
when it does. Try the IP directly to bypass a filtering proxy.

**Want an old hostname (like the Raspberry Pi's) to keep working?**
Once the old machine is unplugged for good, give the container its name
(`pct set $VMID --hostname <old-name>`, then reboot the container). Old Bruno
collections pointing at `http://<old-name>.local:5000` will work again. Don't run both
at once, or they'll fight over the name.

---

## Running it locally

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt pytest httpx
python -m pytest test_server.py -v                    # tests
TEACHER_KEY=dev uvicorn app:app --reload --port 5000  # API + dashboard at http://localhost:5000/dashboard
```

Licensed under GPL-3.0. See [LICENSE](LICENSE).
