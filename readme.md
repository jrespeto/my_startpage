# StartPage (single-file)

A tiny, self-hosted “start.me”-style dashboard you run on `localhost`.
It’s a single Python file (`my_startpage.py`) with a CSV backend—no database, no build step.

---

## What is it?

A personal start page to organize your web life:

* **Pages** (work, personal, hobbies…) each with up to **6 columns**
* **Widgets** (folders) that **group bookmarks & notes**
* **Bookmarks** with site **favicons** and “open all”
* **Notes** with customizable background color
* **Drag-and-drop** reordering of widgets and items
* **Modal forms** in-place for add/edit (no page hops)
* **Bookmark search** popover (shows “bookmark (widget)”, opens in new tab)
* **Dark / light mode** toggle
* **Collapse/expand all** widgets
* **Import** browser `bookmarks.html` (Chrome/Firefox) → folders become widgets
* **Move/Copy/Delete** widgets; **Rename/Delete** pages
* **CSV storage** you can edit with a **small CLI**

---

## What is it for?

* A fast, keyboard-friendly homepage for browsers on your machine
* A portable dashboard that lives in a single folder
* People who want to manage bookmarks & notes without cloud logins or a DB
* Quick bootstraps for teams—drop the CSV in version control

> ⚠️ This is a local tool with a simple admin login. It uses Flask’s development server and is **not hardened for internet exposure**.

---

## How to run it

### 1) Requirements

* **Python 3.10+**
* **Flask** (the only dependency)

```bash
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install Flask
```

### 2) Start the app

```bash
python my_startpage.py
```

Then open: **[http://127.0.0.1:5000](http://127.0.0.1:5000)**

### 3) Log in (to manage content)

* Default credentials: **admin / password**
* Change them via environment variables before starting:

```bash
export ADMIN_USER="me"
export ADMIN_PASS="supersecret"
export FLASK_SECRET="change-this"   # session secret
python my_startpage.py
```

### 4) Use it

* Pick a **Page** from the dropdown (top-left) or create one via **Manage**
* Click a widget’s **menu (≡)** to:

  * Add bookmarks/notes (single or bulk URLs; auto-fetch titles)
  * **Manage bookmarks/notes** (edit/delete inline)
  * **Rename / Move / Copy / Remove** the widget
  * **Open all** bookmarks in new tabs
* Use the top bar:

  * **Search** → opens a popover with results (“bookmark (widget)”)
  * **Collapse/Expand all**, **Dark/Light**
  * **Manage** → page/widget actions, import tools
* The page shows a **total bookmark count**; each widget shows its own count.

### 5) Import browser bookmarks

Export your bookmarks from Chrome/Firefox as **HTML**, then:

* Top bar → **Manage** → **Import bookmarks (HTML)**
* Choose a target page or create a new one
  ⮕ Folders become **widgets**, links go inside the corresponding widget.

---

## CLI (optional)

You can manipulate the CSV from the command line:

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

* All data lives in **`bookmarks.csv`** in the working directory.
* Schema (columns):
  `rowtype,id,page_id,widget_id,column,order,name,url,notes,color`
* Back it up / version it like any file. The app creates it on first run.

---

## Notes / Tips

* Favicons are pulled from `icons.duckduckgo.com` (no extra setup)
* Front-end uses SortableJS via CDN (no install required)
* To change host/port, edit the last line in `my_startpage.py` (`app.run(...)`)
* If you want production hosting, put Flask behind a real WSGI server and set a strong `FLASK_SECRET`. (This project is primarily designed for **localhost**.)

Enjoy!

---

**AI-Generated Code / No Warranty**
This project’s code was generated 100% by **ChatGPT-5 Thinking** (aka “ChatGPT 5”). It is provided **“AS IS,” without warranty of any kind**, express or implied, including but not limited to the warranties of **merchantability**, **fitness for a particular purpose**, and **noninfringement**. In no event shall the author(s) or model provider be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software. **Use at your own risk.**
