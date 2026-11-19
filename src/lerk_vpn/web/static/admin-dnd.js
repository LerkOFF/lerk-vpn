/* Перетаскивание блоков: контейнер data-sortable="<id>", элементы data-key="<ключ>".
   Мышью, пальцем (долгое нажатие) и клавиатурой (Alt + стрелки). Порядок хранится в браузере. */
(function () {
  'use strict';
  var PREFIX = 'lv:order:';
  function storeKey(box) { return PREFIX + location.pathname + ':' + (new URLSearchParams(location.search).get('p') || 'pulse') + ':' + box.dataset.sortable; }
  function items(box) { return Array.prototype.filter.call(box.children, function (el) { return el.dataset && el.dataset.key; }); }
  function save(box) {
    try { localStorage.setItem(storeKey(box), JSON.stringify(items(box).map(function (el) { return el.dataset.key; }))); } catch (e) {}
  }
  function restore(box) {
    var order;
    try { order = JSON.parse(localStorage.getItem(storeKey(box)) || 'null'); } catch (e) { order = null; }
    if (!order) return;
    var map = {}; items(box).forEach(function (el) { map[el.dataset.key] = el; });
    order.forEach(function (k) { if (map[k]) box.appendChild(map[k]); });
    items(box).forEach(function (el) { if (order.indexOf(el.dataset.key) < 0) box.appendChild(el); });
  }
  function setup(box) {
    restore(box);
    items(box).forEach(function (el) {
      el.classList.add('drag');
      el.setAttribute('draggable', 'true');
      el.setAttribute('tabindex', '0');
      el.setAttribute('aria-label', 'Блок можно переместить: перетащите или Alt и стрелки');
      el.addEventListener('dragstart', function (e) {
        if (e.target.closest && e.target.closest('input,textarea,select,a,button') && e.target !== el) { e.preventDefault(); return; }
        box._drag = el; el.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move';
        try { e.dataTransfer.setData('text/plain', el.dataset.key); } catch (x) {}
      });
      el.addEventListener('dragend', function () { el.classList.remove('dragging'); clear(box); box._drag = null; save(box); });
      el.addEventListener('dragover', function (e) {
        if (!box._drag || box._drag === el) return;
        e.preventDefault(); clear(box); el.classList.add('drop-before');
      });
      el.addEventListener('drop', function (e) {
        if (!box._drag || box._drag === el) return;
        e.preventDefault(); box.insertBefore(box._drag, el); clear(box); save(box);
      });
      el.addEventListener('keydown', function (e) {
        if (!e.altKey || e.target !== el) return;
        var k = e.key;
        if (k === 'ArrowUp' || k === 'ArrowLeft') { var p = el.previousElementSibling; if (p) { box.insertBefore(el, p); el.focus(); save(box); } e.preventDefault(); }
        if (k === 'ArrowDown' || k === 'ArrowRight') { var n = el.nextElementSibling; if (n) { box.insertBefore(n, el); el.focus(); save(box); } e.preventDefault(); }
      });
      // палец: долгое нажатие, затем перетаскивание
      var timer = null, active = false;
      el.addEventListener('touchstart', function () { timer = setTimeout(function () { active = true; el.classList.add('dragging'); }, 450); }, { passive: true });
      el.addEventListener('touchmove', function (e) {
        if (!active) { clearTimeout(timer); return; }
        e.preventDefault();
        var t = e.touches[0], over = document.elementFromPoint(t.clientX, t.clientY);
        var target = over && over.closest ? over.closest('[data-key]') : null;
        if (target && target !== el && target.parentNode === box) {
          var before = t.clientY < target.getBoundingClientRect().top + target.offsetHeight / 2;
          box.insertBefore(el, before ? target : target.nextSibling);
        }
      }, { passive: false });
      function end() { clearTimeout(timer); if (active) { active = false; el.classList.remove('dragging'); save(box); } }
      el.addEventListener('touchend', end); el.addEventListener('touchcancel', end);
    });
  }
  function clear(box) { items(box).forEach(function (el) { el.classList.remove('drop-before'); }); }
  document.addEventListener('DOMContentLoaded', function () {
    var boxes = document.querySelectorAll('[data-sortable]');
    Array.prototype.forEach.call(boxes, setup);
    var reset = document.querySelectorAll('[data-dnd-reset]');
    Array.prototype.forEach.call(reset, function (b) {
      b.addEventListener('click', function () {
        Array.prototype.forEach.call(boxes, function (box) { try { localStorage.removeItem(storeKey(box)); } catch (e) {} });
        location.reload();
      });
    });
  });
})();
