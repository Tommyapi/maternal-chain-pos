/* 手机端 Service Worker（APP-405：绝不缓存 api/ 接口，避免停用/调店后继续看到旧数据）。
 * 只缓存手机端自己的静态文件（版本号变化即整体换新）；其他请求（收银台、后台、接口）一律不拦截，直接走网络。 */
var CACHE = 'm-static-3.0.0';
var ASSETS = ['m-app.js', 'm-scan.js', 'manifest.webmanifest', 'favicon.png', 'm-icon.svg'];

self.addEventListener('install', function (e) {
  var base = self.registration.scope;
  e.waitUntil(caches.open(CACHE).then(function (c) {
    return c.addAll(ASSETS.map(function (a) { return new URL('static/' + a, base).toString(); }));
  }).catch(function () {}).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  var scope = new URL(self.registration.scope);
  if (url.origin !== scope.origin) return;
  var rel = url.pathname.slice(scope.pathname.length);
  if (rel.indexOf('api/') === 0) return;                       // 接口：永不缓存
  var name = rel.indexOf('static/') === 0 ? rel.slice(7) : '';
  if (ASSETS.indexOf(name) < 0) return;                         // 非手机端静态文件：不拦截
  // 网络优先，断网时用缓存（门店网络差时页面还能打开，数据仍需联网）
  e.respondWith(fetch(req).then(function (resp) {
    if (resp && resp.ok) {
      var copy = resp.clone();
      caches.open(CACHE).then(function (c) { c.put(req, copy); });
    }
    return resp;
  }).catch(function () { return caches.match(req); }));
});
