var albums = [];
var albumFiles = {};

var IMAGE_BASE = 'https://storage.googleapis.com/colorless-days-children';

function getParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function getAlbum(folder) {
  return albums.find(function(a) { return a.folder === folder; });
}

function getAlbumTotal(album) {
  if (album.useFiles && albumFiles[album.folder]) {
    return albumFiles[album.folder].files.length;
  }
  return album.count;
}

function imageSource(folder, pathName, index, thumbnail) {
  var path = folder + '/';
  if (thumbnail) path += '1_';
  path += pathName;
  if (index < 10) path += '0';
  if (index < 100) path += '0';
  path += index + '.jpg';
  return IMAGE_BASE + '/' + encodeURI(path);
}

function fileImageSource(folder, filename, thumbnail) {
  if (thumbnail) {
    var base = filename.replace(/\.jpg$/i, '');
    return IMAGE_BASE + '/' + encodeURI(folder + '/' + base + '_thumbnail.jpg');
  }
  return IMAGE_BASE + '/' + encodeURI(folder + '/' + filename);
}

function getImageUrl(album, n, thumbnail) {
  if (album.useFiles && albumFiles[album.folder]) {
    var files = albumFiles[album.folder].files;
    var idx = n - 1;
    if (idx < 0 || idx >= files.length) return '';
    return fileImageSource(album.folder, files[idx], thumbnail);
  }
  return imageSource(album.folder, album.pathName, n, thumbnail);
}

var COLORS = [
  'aqua', 'blue', 'blueviolet', 'brown', 'charteuse', 'chocolate',
  'coral', 'crimson', 'cyan', 'darkblue', 'darkgreen', 'darkmagenta',
  'darkorange', 'deeppink', 'firebrick', 'forestgreen', 'green',
  'indigo', 'indianred', 'maroon', 'mediumblue', 'olivedrab', 'orange',
  'orangered', 'purple', 'red', 'sandybrown', 'tomato', 'violet',
  'yellow', 'yellowgreen'
];

function buildHeader(el) {
  var text = '\u0417\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435, \u0414\u0435\u0442\u0438 \u0411\u0435\u0441\u0446\u0432\u0435\u0442\u043d\u044b\u0445 \u0414\u043d\u0435\u0439!';
  var div = document.createElement('div');
  for (var i = 0; i < text.length; i++) {
    var font = document.createElement('font');
    font.textContent = text[i];
    font.size = String(Math.floor(Math.random() * 5) + 3);
    font.color = COLORS[Math.floor(Math.random() * COLORS.length)];
    div.appendChild(font);
  }
  el.appendChild(div);
}

function buildExpander(el) {
  var a = document.createElement('a');
  a.textContent = '1+';
  a.href = 'all.html';
  el.appendChild(a);
  el.appendChild(document.createElement('br'));
}

function buildContent(el, showAll) {
  var visible = albums.filter(function(a) { return getAlbumTotal(a) > 0; });
  var list = showAll ? visible : visible.slice(-10);
  var div = document.createElement('div');
  for (var i = 0; i < list.length; i++) {
    var a = document.createElement('a');
    a.href = 'folderIndex.html?folder=' + encodeURIComponent(list[i].folder);
    a.textContent = list[i].title;
    div.appendChild(a);
    div.appendChild(document.createElement('br'));
  }
  el.appendChild(div);
}

function buildHome(el) {
  el.appendChild(document.createElement('br'));
  var a = document.createElement('a');
  a.textContent = '\u041d\u0430 \u0433\u043b\u0430\u0432\u043d\u0443\u044e.';
  a.href = 'index.html';
  el.appendChild(a);
}

function buildHiking(el) {
  var a = document.createElement('a');
  a.href = 'hiking';
  a.textContent = '\u041f\u043e\u0445\u043e\u0434';
  el.appendChild(a);
  el.appendChild(document.createElement('br'));
}

function buildSpoiler(el) {
  var font = document.createElement('font');
  font.size = '1';
  font.textContent = 'under construction';
  el.appendChild(font);
}

function buildFolderHeader(el) {
  var folder = getParam('folder');
  var album = folder ? getAlbum(folder) : null;
  if (album) {
    var h1 = document.createElement('h1');
    h1.textContent = album.title;
    el.appendChild(h1);
  }
}

function buildFolderTitle() {
  var folder = getParam('folder');
  var album = folder ? getAlbum(folder) : null;
  var titleEl = document.getElementById('folder-title');
  if (album && titleEl) {
    titleEl.textContent = album.title;
  }
}

function buildTable(el) {
  var folder = getParam('folder');
  var album = folder ? getAlbum(folder) : null;
  if (!album) return;
  var first = parseInt(getParam('first') || '0', 10) || 0;
  var total = getAlbumTotal(album);

  var table = document.createElement('table');
  for (var i = 0; i < 4; i++) {
    var tr = table.insertRow();
    for (var j = 1; j <= 4; j++) {
      var n = first + i * 4 + j;
      if (n <= total) {
        var img = document.createElement('img');
        img.src = getImageUrl(album, n, true);
        var a = document.createElement('a');
        a.appendChild(img);
        a.href = 'preview.html?folder=' + encodeURIComponent(folder) + '&n=' + n;
        var td = tr.insertCell();
        td.appendChild(a);
      }
    }
  }
  if (total <= 0) {
    var tr2 = table.insertRow();
    var td2 = tr2.insertCell();
    td2.colSpan = 4;
    td2.textContent = '\u042d\u0442\u0438 \u0444\u043e\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u0438 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b';
  }
  var navRow = table.insertRow();
  var tdPrev = navRow.insertCell();
  if (first !== 0) {
    var nfirst = first < 16 ? 0 : first - 16;
    var prevLink = document.createElement('a');
    prevLink.href = 'folderIndex.html?folder=' + encodeURIComponent(folder) + '&first=' + nfirst;
    prevLink.textContent = '\u041f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0438\u0435 \u0444\u043e\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u0438';
    tdPrev.appendChild(prevLink);
  }
  var tdUp = navRow.insertCell();
  tdUp.colSpan = 2;
  var upLink = document.createElement('a');
  upLink.href = 'index.html';
  upLink.textContent = '\u0412\u0432\u0435\u0440\u0445';
  tdUp.appendChild(upLink);
  var tdNext = navRow.insertCell();
  if (first + 16 < total) {
    var nextLink = document.createElement('a');
    nextLink.href = 'folderIndex.html?folder=' + encodeURIComponent(folder) + '&first=' + (first + 16);
    nextLink.textContent = '\u0421\u043b\u0435\u0434\u0443\u0449\u0438\u0435 \u0444\u043e\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u0438';
    tdNext.appendChild(nextLink);
  }
  el.appendChild(table);
}

function buildPreviewPanel(el) {
  var folder = getParam('folder');
  var n = parseInt(getParam('n') || '0', 10) || 0;
  var album = folder ? getAlbum(folder) : null;
  if (!album || n <= 0) return;
  var total = getAlbumTotal(album);

  var table = document.createElement('table');
  var trPic = table.insertRow();
  var img = document.createElement('img');
  img.src = getImageUrl(album, n, false);
  img.className = 'preview-image';
  var imgLink = document.createElement('a');
  imgLink.appendChild(img);
  imgLink.href = 'preview.html?folder=' + encodeURIComponent(folder) + '&n=' + (n % total + 1);
  var tdPic = trPic.insertCell();
  tdPic.colSpan = 3;
  tdPic.appendChild(imgLink);

  var trNav = table.insertRow();
  var tdPrev = trNav.insertCell();
  if (n > 1) {
    var prevImg = document.createElement('img');
    prevImg.src = getImageUrl(album, n - 1, true);
    prevImg.className = 'thumbnail-image';
    var prevLink = document.createElement('a');
    prevLink.appendChild(prevImg);
    prevLink.href = 'preview.html?folder=' + encodeURIComponent(folder) + '&n=' + (n - 1);
    tdPrev.appendChild(prevLink);
  }
  var tdUp = trNav.insertCell();
  var upLink = document.createElement('a');
  upLink.href = 'folderIndex.html?folder=' + encodeURIComponent(folder) + '&first=' + (n - 1);
  upLink.textContent = '\u0412\u0432\u0435\u0440\u0445';
  tdUp.appendChild(upLink);
  var tdNext = trNav.insertCell();
  if (n < total) {
    var nextImg = document.createElement('img');
    nextImg.src = getImageUrl(album, n + 1, true);
    nextImg.className = 'thumbnail-image';
    var nextLink = document.createElement('a');
    nextLink.appendChild(nextImg);
    nextLink.href = 'preview.html?folder=' + encodeURIComponent(folder) + '&n=' + (n + 1);
    tdNext.appendChild(nextLink);
  }
  el.appendChild(table);
}

function init(data) {
  albums = data;
  var el;
  if ((el = document.getElementById('header'))) buildHeader(el);
  if ((el = document.getElementById('expander'))) buildExpander(el);
  if ((el = document.getElementById('content'))) {
    var showAll = !document.getElementById('expander');
    buildContent(el, showAll);
  }
  if ((el = document.getElementById('home'))) buildHome(el);
  if ((el = document.getElementById('hiking'))) buildHiking(el);
  if ((el = document.getElementById('spoiler'))) buildSpoiler(el);
  if ((el = document.getElementById('folder-header'))) buildFolderHeader(el);
  buildFolderTitle();
  if ((el = document.getElementById('table'))) buildTable(el);
  if ((el = document.getElementById('preview-panel'))) buildPreviewPanel(el);
}

Promise.all([
  fetch('albums.json').then(function(r) { return r.json(); }),
  fetch('albums-files.json').then(function(r) { return r.json(); })
]).then(function(results) {
  albumFiles = results[1];
  init(results[0]);
});
