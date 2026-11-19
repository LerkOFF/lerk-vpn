(function () {
  'use strict';
  var root = document.documentElement;
  // тема
  try { var t = localStorage.getItem('lv:theme'); if (t) root.setAttribute('data-theme', t); } catch (e) {}
  document.addEventListener('DOMContentLoaded', function () {
    var tb = document.getElementById('theme');
    if (tb) tb.addEventListener('click', function () {
      var dark = root.getAttribute('data-theme') === 'dark' || (!root.getAttribute('data-theme') && matchMedia('(prefers-color-scheme: dark)').matches);
      var next = dark ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('lv:theme', next); } catch (e) {}
    });
    // опасные кнопки: подтверждение вторым нажатием
    Array.prototype.forEach.call(document.querySelectorAll('[data-confirm]'), function (b) {
      b.addEventListener('click', function (e) {
        if (b.dataset.armed === '1') return;
        e.preventDefault();
        b.dataset.armed = '1'; b.dataset.label = b.textContent; b.textContent = b.dataset.confirm; b.classList.add('confirm');
        setTimeout(function () { b.dataset.armed = ''; b.textContent = b.dataset.label; b.classList.remove('confirm'); }, 4000);
      });
    });
    // копирование
    Array.prototype.forEach.call(document.querySelectorAll('[data-copy]'), function (b) {
      b.addEventListener('click', function () {
        var el = document.getElementById(b.dataset.copy); if (!el) return;
        var text = el.dataset.value || el.textContent;
        (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () { flash(b); }, function () {
          var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
          try { document.execCommand('copy'); flash(b); } catch (e) {} document.body.removeChild(ta);
        });
      });
    });
    function flash(b) { var o = b.textContent; b.textContent = 'Скопировано'; setTimeout(function () { b.textContent = o; }, 1400); }
    // скрытое до нажатия
    Array.prototype.forEach.call(document.querySelectorAll('[data-reveal]'), function (b) {
      b.addEventListener('click', function () {
        var el = document.getElementById(b.dataset.reveal); if (!el) return;
        el.textContent = el.dataset.value; b.remove();
      });
    });
    // быстрый поиск
    document.addEventListener('keydown', function (e) {
      var s = document.getElementById('q'); if (!s) return;
      var inField = /input|textarea|select/i.test((e.target.tagName || ''));
      if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') || (!inField && e.key === '/')) { e.preventDefault(); s.focus(); s.select(); }
    });
    // живой журнал задачи
    var log = document.getElementById('joblog');
    if (log && log.dataset.running === '1') {
      var id = log.dataset.id;
      var tick = function () {
        fetch('/?p=job&id=' + id + '&raw=1', { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
          log.textContent = j.log || '';
          log.scrollTop = log.scrollHeight;
          var st = document.getElementById('jobstatus'); if (st) st.textContent = j.status_label;
          if (j.status === 'running') setTimeout(tick, 1800); else location.reload();
        }).catch(function () { setTimeout(tick, 4000); });
      };
      setTimeout(tick, 1200);
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-print]'), function (b) { b.addEventListener('click', function () { window.print(); }); });
    // после установки обновления ждём, пока панель поднимется
    if (/[?&]restarting=1/.test(location.search)) {
      var wait = function () {
        fetch('/health', { cache: 'no-store' }).then(function (r) { if (r.ok) location.replace('/?p=settings'); else setTimeout(wait, 1500); }).catch(function () { setTimeout(wait, 1500); });
      };
      setTimeout(wait, 2500);
    }
    // конструктор цепочки: любое число серверов, последний выходной
    Array.prototype.forEach.call(document.querySelectorAll('[data-hops]'), function (box) {
      var list = box.querySelector('[data-hop-list]'), tpl = box.querySelector('[data-hop-tpl]');
      var entry = box.querySelector('[data-hop-entry]'), exit = box.querySelector('[data-hop-exit]'), pv = box.querySelector('[data-hop-preview]');
      function label(sel) { var o = sel.options[sel.selectedIndex]; return o && o.value ? o.value : '?'; }
      function preview() {
        var parts = [label(entry)];
        Array.prototype.forEach.call(list.querySelectorAll('select'), function (s) { if (s.value) parts.push(label(s)); });
        parts.push(label(exit));
        var dup = parts.some(function (p, i) { return parts.indexOf(p) !== i; });
        pv.textContent = 'Путь: клиент → ' + parts.join(' → ') + ' → интернет' + (dup ? '. Сервер повторяется, так нельзя.' : '. Выход в сеть будет с адреса ' + parts[parts.length - 1] + '.');
      }
      box.addEventListener('change', preview);
      box.querySelector('[data-hop-add]').addEventListener('click', function () {
        list.appendChild(tpl.content.cloneNode(true)); preview();
      });
      list.addEventListener('click', function (e) {
        var row = e.target.closest('.hoprow'); if (!row) return;
        if (e.target.matches('[data-hop-del]')) row.remove();
        else if (e.target.matches('[data-hop-up]') && row.previousElementSibling) list.insertBefore(row, row.previousElementSibling);
        else if (e.target.matches('[data-hop-down]') && row.nextElementSibling) list.insertBefore(row.nextElementSibling, row);
        preview();
      });
      preview();
    });
    // выбор сервера 2: ровно один
    // (радио-кнопки это обеспечивают сами, для сервера 1 допустим список)
  });
})();
