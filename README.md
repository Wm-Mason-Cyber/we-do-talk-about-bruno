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

## Setting up the LXC (from scratch)

Everything below runs in **one shell on a Proxmox node** (web UI → node → **>_ Shell**,
or `ssh root@<node>`). Keep that shell open from start to finish, because each step
uses the variables set in step 1. If you lose the shell, paste step 1 again.

### Before you start: two decisions

**1. Which network?** The container has to be on a network the classroom laptops can reach.

- **Best:** the same VLAN/subnet as the classroom laptops. Then `bruno-api.local`
  resolves through mDNS with no setup.
- **OK:** a different subnet that's routed to the classroom. mDNS will **not** cross
  subnets, so students use the IP address, or IT adds a DNS record.
- **Won't work:** a management/server VLAN that classroom devices can't route to.

**2. Which IP address?** Pick one **static address that nothing else uses**, ideally
outside the router's DHCP range, so the router can never hand it to another device.
Don't rely on DHCP: if the router gives the container an address another server is
already using, students' requests land on that server instead (this has happened
here once already, with a GitLab VM). Step 2 checks that your pick is free before
anything gets created.

### Starting over? Remove the old container first

```bash
OLD=120                          # the old container's ID
pct stop $OLD; pct destroy $OLD --purge
```

`--purge` also removes it from HA and backup jobs. Skip this on a first install.

### 1. Set your values

```bash
# ── adjust these ──────────────────────────────────────────────
VMID=120                 # an unused ID — `pvesh get /cluster/nextid` suggests one
ADDR=10.24.81.N          # the static IP you picked (must be unused!)
CIDR=24                  # subnet size: 24 = 255.255.255.0
GW=10.24.81.1            # your router/gateway
BRIDGE=vmbr0             # the bridge that reaches the classroom network
VLAN=                    # e.g. 112 if the classroom is a tagged VLAN; blank if not
STORAGE=local-lvm        # where the root disk lives (local-lvm, ceph pool, etc.)
HOSTNAME=bruno-api       # becomes  http://bruno-api.local
# ──────────────────────────────────────────────────────────────
```

### 2. Check that the ID and IP are free

```bash
if [ -n "$(ls /etc/pve/nodes/*/{lxc,qemu-server}/$VMID.conf 2>/dev/null)" ]; then
  echo "❌ ID $VMID is already used — pick another"
else
  echo "✅ ID $VMID is free"
fi
if ! [[ $ADDR =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "❌ ADDR=$ADDR isn't a real IP address yet — edit step 1"
else
  ping -c2 -W1 "$ADDR" &>/dev/null
  if ip neigh show "$ADDR" | grep -q lladdr; then
    echo "❌ $ADDR is already in use by MAC $(ip neigh show "$ADDR" | awk '{print $5}') — pick another"
  else
    echo "✅ nothing answers at $ADDR"
  fi
fi
```

Only continue when both lines are ✅. (The IP check works because the Proxmox host is on
the same network. A device that ignores ping still shows up in `ip neigh`.)

### 3. Create and start the container

```bash
pveam update
TEMPLATE=$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)
echo "Using template: $TEMPLATE"
pveam download local "$TEMPLATE"

pct create "$VMID" "local:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores 1 --memory 512 --swap 512 \
  --rootfs "$STORAGE:4" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$ADDR/$CIDR,gw=$GW${VLAN:+,tag=$VLAN}" \
  --nameserver "$GW" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 \
  --description "Bruno lab: Class Roster API + live dashboard — github.com/Wm-Mason-Cyber/we-do-talk-about-bruno"

pct start "$VMID"
sleep 5
pct exec "$VMID" -- timeout 5 bash -c 'exec 3<>/dev/tcp/github.com/443' \
  && echo "✅ container can reach GitHub" \
  || echo "❌ container can't reach the internet — check GW / VLAN / nameserver"
```

`--nameserver` assumes your gateway also answers DNS, which most routers do. If yours
doesn't, use your school's DNS server's IP instead.

Notes:

- **Debian 12** is used on purpose. It works on both Proxmox VE 8 and 9. The app also
  runs on Debian 13 (CI tests both Python versions).
- `nesting=1` is what the Proxmox GUI sets by default for unprivileged containers.
  Recent Debian's systemd needs it.
- 1 core, 512 MB RAM, and a 4 GB disk is plenty. The app idles at about 60 MB.
- **Prefer the GUI?** *Create CT* with the same settings: hostname `bruno-api`, Debian 12
  template, 4 GB disk, 1 core, 512 MB, **static** IPv4 `ADDR/CIDR` with your gateway,
  your DNS server, and *Start at boot* ticked under Options afterward. Then continue
  with step 4 here.

### 4. Install the app from GitHub

```bash
pct exec "$VMID" -- env LC_ALL=C.UTF-8 bash -c 'apt-get update -qq && apt-get install -y -qq curl >/dev/null && \
  curl -fsSL https://raw.githubusercontent.com/Wm-Mason-Cyber/we-do-talk-about-bruno/main/deploy/install.sh | bash'
```

This takes a minute or two. It clones this repo to `/opt/bruno-api/src`, sets
everything up, and **finishes with a box listing the URLs and your teacher key**. If it
stops before that box, the last lines say what went wrong. Fix that, then run the same
command again; it's safe to repeat.

You need the teacher key for the dashboard's *Teacher* button. To see it again later:

```bash
pct exec "$VMID" -- cat /etc/bruno-api.env
```

The container needs outbound internet to reach GitHub, apt, and PyPI when installing
or updating. While it's serving the class, it doesn't.

### 5. Verify you're reaching *this* server

From the Proxmox shell:

```bash
curl -s -D - -o /dev/null http://$ADDR:5000/students | grep -i '^x-served-by' && echo "✅ port 5000 OK"
curl -s -D - -o /dev/null http://$ADDR/dashboard     | grep -i '^x-served-by' && echo "✅ port 80 OK"
```

Both lines should print `X-Served-By: bruno-api`. If one doesn't, something else on
the network is answering at that address (see Troubleshooting).

Then from a **student laptop** and the **projector computer**:

1. `http://bruno-api.local:5000/students` shows JSON with Ada and Grace. If the name
   doesn't resolve, use `http://<ADDR>:5000/students`, and see Troubleshooting.
2. `http://bruno-api.local/dashboard` on the projector: the green dot reads **live**.
3. In Bruno, `POST /students` with `{"name": "test"}`. It appears on the dashboard within
   about a second.

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
steps 1–5.

---

## Troubleshooting

**`bruno-api.local` doesn't resolve, but the IP works.**
mDNS only works within one subnet/VLAN. Either the container is on a different subnet
from the laptops (see step 0), or the network blocks multicast. Use the IP in the
student instructions, or ask IT for a DNS A record. Check that avahi is running with
`systemctl status avahi-daemon` inside the container.

**The IP opens some *other* server (a login page, another app), or the container "flickers" up and down.**
Two machines are using the same IP address, and the network keeps switching which one
gets the traffic. The installer warns about this. Check from the Proxmox host:
`ip neigh show <IP>` should show the same MAC as `pct config $VMID | grep hwaddr`.
Give the container an address nobody else uses, outside the DHCP pool (or reserve one
in DHCP):

```bash
pct set $VMID --net0 "name=eth0,bridge=vmbr0,ip=10.x.x.N/24,gw=10.x.x.1"   # add ,tag=<vlan> if used
pct reboot $VMID
```

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
