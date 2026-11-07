/* 手机端摄像头扫码（APP-102 / APP-201）。
 * 识别顺序：1) 浏览器原生 BarcodeDetector（安卓 Chrome/Edge、鸿蒙浏览器内核较新的版本）；
 *          2) 本地 ZXing（若服务器放了 static/vendor/zxing.min.js 就按需加载，不连外网）；
 *          3) 都没有：手动输入 / 蓝牙扫码枪（扫码枪等同键盘输入 + 回车）。
 * 用法：MScan.open(function (code) { ... }, {title: '扫描商品条码'})
 */
(function (w) {
  'use strict';
  var FORMATS = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'itf', 'qr_code'];
  var zxingTried = false;

  function el(html) {
    var d = document.createElement('div');
    d.innerHTML = html;
    return d.firstElementChild;
  }

  function loadZXing(cb) {
    if (w.ZXingBrowser || w.ZXing) return cb(true);
    if (zxingTried) return cb(false);
    zxingTried = true;
    var s = document.createElement('script');
    s.src = 'static/vendor/zxing.min.js';
    s.onload = function () { cb(!!(w.ZXingBrowser || w.ZXing)); };
    s.onerror = function () { cb(false); };
    document.head.appendChild(s);
  }

  function supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function open(onCode, opt) {
    opt = opt || {};
    var done = false, stream = null, timer = null, zreader = null, zctl = null;
    var box = el('<div class="scan"><video playsinline muted autoplay></video><div class="frame"></div>' +
      '<div class="tip">' + (opt.title || '把条码放进框内') + '</div>' +
      '<div class="bar"><input class="inp" placeholder="或手动输入 / 扫码枪扫码" enterkeyhint="done">' +
      '<button class="btn sm">确定</button><button class="btn sm gray">关闭</button></div></div>');
    document.body.appendChild(box);
    var video = box.querySelector('video'), tip = box.querySelector('.tip'), inp = box.querySelector('input');
    var btns = box.querySelectorAll('button');

    function close() {
      done = true;
      if (timer) clearTimeout(timer);
      try { if (zctl && zctl.stop) zctl.stop(); else if (zreader && zreader.reset) zreader.reset(); } catch (e) { /* ignore */ }
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      if (box.parentNode) box.parentNode.removeChild(box);
    }

    function hit(code) {
      code = String(code || '').trim();
      if (!code || done) return;
      try { if (navigator.vibrate) navigator.vibrate(60); } catch (e) { /* ignore */ }
      close();
      onCode(code);
    }

    btns[0].onclick = function () { hit(inp.value); };
    btns[1].onclick = close;
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') hit(inp.value); });

    if (!supported()) {
      tip.textContent = '当前浏览器不能调用摄像头（需 HTTPS），请手动输入或用扫码枪';
      inp.focus();
      return { close: close };
    }

    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 } }, audio: false })
      .then(function (s) {
        stream = s;
        if (done) { close(); return; }
        video.srcObject = s;
        return video.play();
      })
      .then(function () {
        if (done) return;
        if ('BarcodeDetector' in w) {
          var det;
          try { det = new w.BarcodeDetector({ formats: FORMATS }); } catch (e) { det = new w.BarcodeDetector(); }
          var loop = function () {
            if (done) return;
            det.detect(video).then(function (rs) {
              if (rs && rs.length) hit(rs[0].rawValue);
              else timer = setTimeout(loop, 180);
            }).catch(function () { timer = setTimeout(loop, 400); });
          };
          loop();
          return;
        }
        loadZXing(function (ok) {
          if (done) return;
          if (!ok) {
            tip.textContent = '此浏览器不支持自动识别条码，请手动输入或用扫码枪';
            return;
          }
          try {
            var Z = w.ZXingBrowser || w.ZXing;
            zreader = new Z.BrowserMultiFormatReader();
            var pr = zreader.decodeFromVideoElement(video, function (res) { if (res) hit(res.getText ? res.getText() : res.text); });
            // @zxing/browser 0.2.x 没有 reset()，要用返回的 controls.stop() 停止解码循环
            if (pr && pr.then) pr.then(function (c) { zctl = c; if (done && c && c.stop) c.stop(); }).catch(function () {});
          } catch (e) {
            tip.textContent = '扫码组件启动失败，请手动输入';
          }
        });
      })
      .catch(function () {
        tip.textContent = '没有拿到摄像头权限，请在浏览器设置里允许，或手动输入';
        inp.focus();
      });
    return { close: close };
  }

  w.MScan = { open: open, supported: supported };
})(window);
