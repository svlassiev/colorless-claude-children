FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html all.html folderIndex.html preview.html /usr/share/nginx/html/
COPY styles.css app.js albums.json albums-files.json icon.png /usr/share/nginx/html/
COPY google73c9418809a5ba07.html /usr/share/nginx/html/
COPY cv/ /usr/share/nginx/html/cv/
