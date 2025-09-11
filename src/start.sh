#!/bin/sh
set -e

cd /data

# Initialize CSV if missing (pure shell, no inline Python)
if [ ! -f bookmarks.csv ]; then
  echo 'rowtype,id,page_id,widget_id,column,order,name,url,notes,color' > bookmarks.csv
  echo 'page,home,,,,0,My Start Page,,,' >> bookmarks.csv
fi

# Start the app
exec gunicorn -b 0.0.0.0:5000 -w "${WORKERS:-2}" my_startpage:app
