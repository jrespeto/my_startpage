````markdown
# StartPage (single-file)

A tiny, self-hosted **“start.me”-style** dashboard you run on `localhost`.
It’s a single Python file—**`my_startpage.py`**—with a CSV backend (no database, no build step).
Optional Docker/Compose setup is included.

---

## Features

- **Pages** (work, personal, hobbies…) with up to **6 columns**
- **Widgets** (folders) that **group bookmarks & notes**
- **Bookmarks** with site **favicons** and an **“Open all”** action
- **Notes** with customizable **background color**
- **Drag-and-drop** reordering of widgets and items
- **In-place modals** to add/edit (no page hops)
- **Search popover** (shows *bookmark (widget)*; opens in a new tab)
- **Dark / Light mode** toggle
- **Collapse/Expand all** widgets
- **Import** Chrome/Firefox `bookmarks.html` → folders become widgets
- **Move / Copy / Delete** widgets; **Rename / Delete** pages
- **CSV storage** with a small **CLI** for quick edits

> ⚠️ Designed for local use with a simple admin login. Not hardened for public internet exposure.

---

## What is it for?

- A fast, keyboard-friendly browser start page
- A portable dashboard that lives in a single folder
- Managing bookmarks & notes without cloud logins or a DB
- Quick team bootstraps—drop the CSV in version control

---

## How to run it (System / Python)

### 1) Prerequisites
- **Python 3.10+**
- **Flask**

```bash
python -m venv .venv
. .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install Flask
````

### 2) Start the app

```bash
python my_startpage.py
```

Open: **[http://127.0.0.1:5000](http://127.0.0.1:5000)**

### 3) Log in to manage

Default credentials: **admin / password**
Change them via environment variables:

```bash
export ADMIN_USER="me"
export ADMIN_PASS="supersecret"
export FLASK_SECRET="change-this"  # session secret
python my_startpage.py
```

### 4) Use it

* Choose a **Page** from the top-left dropdown (or add one via **Manage**)
* Widget **menu (≡)** → add bookmarks/notes (single or bulk w/ auto titles), manage items, rename/move/copy/delete, open all
* Top bar: **Search** (popover), **Collapse/Expand all**, **Dark/Light**, **Manage**
* See total bookmark count (page) and per-widget counts

### 5) Import browser bookmarks

Export from Chrome/Firefox as **HTML**, then:
**Manage → Import bookmarks (HTML)** → choose target page (or create one).
Folders become widgets, links go inside.

---

## Optional: Run with Docker Compose

This runs the app in a container behind **gunicorn**.
All data (`bookmarks.csv`) is stored in a bind-mounted host folder.

### Prerequisites

* Docker (Engine 20+)
* Docker Compose v2 (`docker compose ...`)

### Files

* `Containerfile` — build recipe (aka Dockerfile)
* `start.sh` — entrypoint (creates `bookmarks.csv` if missing; starts gunicorn)
* `container-compose.yml` — compose definition (service, ports, volume, env)

> Prefer `docker-compose.yml`? Rename the file and drop the `-f` flag in commands.

### Quick start

```bash
# 1) Build and start
docker compose -f container-compose.yml up --build -d

# 2) Open
# http://localhost:5000

# 3) Logs (optional)
docker compose -f container-compose.yml logs -f

# 4) Stop
docker compose -f container-compose.yml down
```

On first start, the container creates `/data/bookmarks.csv` if it doesn’t exist.
`./data` on your host is mounted to `/data` in the container to persist it.

### Configuration (env)

Set in `container-compose.yml` or a `.env` file:

* `ADMIN_USER` – admin username (default: `admin`)
* `ADMIN_PASS` – admin password (default: `password`)
* `FLASK_SECRET` – session secret (change this!)
* `WORKERS` – gunicorn workers (default: `2`)

Example `.env`:

```env
ADMIN_USER=me
ADMIN_PASS=supersecret
FLASK_SECRET=please-change-this
WORKERS=3
```

Start with:

```bash
docker compose -f container-compose.yml up -d
```

### Data & backups (container)

```
./data/            # host
  └─ bookmarks.csv # created on first run
```

Back up:

```bash
cp -a ./data ./data.backup
```

Restore by replacing `./data` while the stack is stopped.

### Custom ports

Change the **left** side (host) in the compose file:

```yaml
ports:
  - "8080:5000"  # host:container
```

Then:

```bash
docker compose -f container-compose.yml up -d
# open http://localhost:8080
```

### Updating the container

* If you changed `my_startpage.py`:

  ```bash
  docker compose -f container-compose.yml up --build -d
  ```
* If you only changed env:

  ```bash
  docker compose -f container-compose.yml up -d
  ```

### Importing bookmarks in a container

Use the web UI: **Manage → Import bookmarks (HTML)**.
(You can also drop files into `./data/` on the host and select them in the form.)

---

## CLI (optional)

Manipulate the CSV from the command line:

```bash
# List pages
python my_startpage.py list-pages

# List widgets on a page
python my_startpage.py list-widgets --page <PAGE_ID>

# Add / rename / delete a widget
python my_startpage.py add-widget --page <PAGE_ID> --name "Reading" --column 3
python my_startpage.py rename-widget --id <WIDGET_ID> --name "Daily Reads"
python my_startpage.py delete-widget --id <WIDGET_ID>

# Add a bookmark or a note
python my_startpage.py add-bookmark --widget <WID> --url https://example.com --name "Example"
python my_startpage.py add-note --widget <WID> --text "Sticky note" --color "#FEF3C7"

# Delete an item (bookmark or note)
python my_startpage.py delete-item --id <ITEM_ID>

# Import a bookmarks.html file
python my_startpage.py import --file ~/Downloads/bookmarks.html --new-page "Imported" --column-start 1
```

---

## Data & portability

* All data lives in **`bookmarks.csv`** in the working directory (or `/data` in the container)
* CSV schema:
  `rowtype,id,page_id,widget_id,column,order,name,url,notes,color`
* Back up/version the CSV like any file. It’s created on first run.

---

## Notes / Tips

* Press **/** to open the search popover quickly
* Favicons are fetched from `icons.duckduckgo.com` (no extra setup)
* SortableJS is loaded via CDN (no install required)
* To tweak host/port in system mode, edit the last line of `my_startpage.py` (`app.run(...)`)
* For production-style hosting, use a real WSGI server/reverse proxy and a strong `FLASK_SECRET`.
  (This project targets **localhost**.)

---

## AI-Generated Code / No Warranty

This project’s code was generated **100% by ChatGPT-5 Thinking** (“ChatGPT 5”).
It is provided **“AS IS,” without warranty of any kind**, express or implied, including but not limited to the warranties of **merchantability**, **fitness for a particular purpose**, and **noninfringement**. In no event shall the author(s) or model provider be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software. **Use at your own risk.**

