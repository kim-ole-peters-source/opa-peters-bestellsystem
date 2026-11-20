/* Verwaltung Opa Peters – Bedienlogik (November 2026) */
(function () {
  'use strict';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function norm(text) {
    return (text || '').toLowerCase()
      .replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue').replace(/ß/g, 'ss')
      .replace(/[^a-z0-9]+/g, ' ').trim();
  }

  /* ---------- Fenster (Dialoge) ---------- */
  function oeffnen(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === 'function') { if (!dialog.open) dialog.showModal(); }
    else dialog.setAttribute('open', '');
  }
  function schliessen(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
  }
  document.addEventListener('click', function (e) {
    var auf = e.target.closest('[data-dialog]');
    if (auf) { e.preventDefault(); oeffnen(document.getElementById(auf.getAttribute('data-dialog'))); return; }
    var zu = e.target.closest('[data-schliessen]');
    if (zu) { e.preventDefault(); schliessen(zu.closest('dialog')); return; }
    if (e.target.tagName === 'DIALOG' && e.target.classList.contains('vw-dialog')) {
      var r = e.target.getBoundingClientRect();
      var drin = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      if (!drin) schliessen(e.target);
    }
  });

  /* ---------- Fenster, die sich beim Laden öffnen sollen ---------- */
  $$('dialog[data-auto-oeffnen]').forEach(oeffnen);

  /* ---------- Ganze Tabellenzeile anklickbar ---------- */
  document.addEventListener('click', function (e) {
    var zeile = e.target.closest('tr[data-href]');
    if (zeile && !e.target.closest('a, button, input, select, label')) window.location.href = zeile.getAttribute('data-href');
  });

  /* ---------- Rückfragen vor dem Löschen ---------- */
  document.addEventListener('click', function (e) {
    var knopf = e.target.closest('button[data-vw-confirm]');
    if (knopf && !window.confirm(knopf.getAttribute('data-vw-confirm'))) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.hasAttribute && form.hasAttribute('data-vw-confirm') && !window.confirm(form.getAttribute('data-vw-confirm'))) e.preventDefault();
  }, true);

  /* ---------- Auswahlfeld schickt Formular sofort ab ---------- */
  document.addEventListener('change', function (e) {
    if (e.target.matches && e.target.matches('[data-auto-submit]')) e.target.form.submit();
  });

  /* ---------- Reiter-Leiste: aktiven Reiter sichtbar scrollen ---------- */
  $$('.vw-reiter [aria-current="page"]').forEach(function (a) {
    if (a.scrollIntoView) a.scrollIntoView({ block: 'nearest', inline: 'center' });
  });

  /* ---------- Produkte ---------- */
  var liste = $('#vwProduktliste');
  if (liste) {
    var zeilen = $$('.vw-produkt', liste);
    var suche = $('#vwProduktSuche');
    var haendler = $('#vwHaendlerFilter');
    var status = $('#vwStatusFilter');
    var chips = $$('#vwGruppenChips .vw-chip');
    var keinTreffer = $('#vwKeinTreffer');
    var gruppe = '';

    function filtern() {
      var worte = norm(suche ? suche.value : '').split(' ').filter(Boolean);
      var h = haendler ? haendler.value : '';
      var st = status ? status.value : '';
      var sichtbar = 0;
      zeilen.forEach(function (z) {
        var text = z.getAttribute('data-suche') || '';
        var ok = worte.every(function (w) { return text.indexOf(w) !== -1; })
          && (!gruppe || (' ' + z.getAttribute('data-gruppen') + ' ').indexOf(' ' + gruppe + ' ') !== -1)
          && (!h || z.getAttribute('data-haendler') === h)
          && (!st || z.getAttribute('data-status') === st);
        z.hidden = !ok;
        if (ok) sichtbar++;
      });
      if (keinTreffer) keinTreffer.hidden = sichtbar !== 0 || !zeilen.length;
      auswahlZaehlen();
    }
    if (suche) suche.addEventListener('input', filtern);
    if (haendler) haendler.addEventListener('change', filtern);
    if (status) status.addEventListener('change', filtern);
    chips.forEach(function (chip) {
      chip.addEventListener('click', function () {
        gruppe = chip.getAttribute('data-gruppe') || '';
        chips.forEach(function (c) { c.setAttribute('aria-pressed', c === chip ? 'true' : 'false'); });
        filtern();
      });
    });

    var alle = $('#vwAlleWaehlen');
    var leiste = $('#vwMassenleiste');
    var zahl = $('#vwAuswahlZahl');
    function auswahlZaehlen() {
      var boxen = $$('.vw-produkt-check', liste);
      var gewaehlt = boxen.filter(function (b) { return b.checked; });
      zeilen.forEach(function (z) { z.classList.toggle('ist-gewaehlt', $('.vw-produkt-check', z).checked); });
      if (zahl) zahl.textContent = gewaehlt.length + (gewaehlt.length === 1 ? ' Produkt ausgewählt' : ' Produkte ausgewählt');
      if (leiste) leiste.hidden = gewaehlt.length === 0;
      document.documentElement.classList.toggle('vw-hat-auswahl', gewaehlt.length > 0);
      if (alle) {
        var sichtbare = boxen.filter(function (b) { return !b.closest('.vw-produkt').hidden; });
        var an = sichtbare.filter(function (b) { return b.checked; }).length;
        alle.checked = sichtbare.length > 0 && an === sichtbare.length;
        alle.indeterminate = an > 0 && an < sichtbare.length;
      }
    }
    liste.addEventListener('change', function (e) { if (e.target.classList.contains('vw-produkt-check')) auswahlZaehlen(); });
    if (alle) alle.addEventListener('change', function () {
      zeilen.forEach(function (z) { if (!z.hidden) $('.vw-produkt-check', z).checked = alle.checked; });
      auswahlZaehlen();
    });
    var aufheben = $('#vwAuswahlAufheben');
    if (aufheben) aufheben.addEventListener('click', function () {
      $$('.vw-produkt-check', liste).forEach(function (b) { b.checked = false; });
      auswahlZaehlen();
    });
    var massenSicht = $('#vwMassenSicht');
    var massenStandorte = $('#vwMassenStandorte');
    if (massenSicht && massenStandorte) massenSicht.addEventListener('change', function () {
      massenStandorte.hidden = massenSicht.value !== 'custom';
    });

    /* Produkt im Seitenfenster bearbeiten */
    var panel = $('#vwProduktPanel');
    var inhalt = $('#vwProduktPanelInhalt');
    liste.addEventListener('click', function (e) {
      var knopf = e.target.closest('.vw-produkt-knopf');
      if (!knopf || !panel) return;
      var id = knopf.getAttribute('data-produkt');
      inhalt.innerHTML = '<p class="vw-leer">Wird geladen …</p>';
      oeffnen(panel);
      fetch('/admin/edit-product?embed=1&id=' + encodeURIComponent(id), { credentials: 'same-origin' })
        .then(function (r) { return r.text(); })
        .then(function (html) { inhalt.innerHTML = html; var f = $('input[name="name"]', inhalt); if (f) f.focus(); })
        .catch(function () { window.location.href = '/admin/edit-product?id=' + encodeURIComponent(id); });
    });

    var startGruppe = new URLSearchParams(window.location.search).get('gruppe');
    if (startGruppe) chips.forEach(function (c) { if (c.getAttribute('data-gruppe') === startGruppe) c.click(); });
    filtern();
    var ziel = window.location.hash && document.getElementById(window.location.hash.slice(1));
    if (ziel) { ziel.classList.add('ist-markiert'); ziel.scrollIntoView({ block: 'center' }); }
  }

  /* ---------- Bestellungen: Sammelauswahl ---------- */
  var sammelLeiste = $('#vwSammelLeiste');
  if (sammelLeiste) {
    var sammelZahl = $('#vwSammelZahl');
    var zaehleBestellungen = function () {
      var n = $$('.vw-bestell-check').filter(function (b) { return b.checked; }).length;
      sammelLeiste.hidden = n === 0;
      document.documentElement.classList.toggle('vw-hat-auswahl', n > 0);
      if (sammelZahl) sammelZahl.textContent = n + (n === 1 ? ' Bestellung ausgewählt' : ' Bestellungen ausgewählt');
      $$('.vw-bestell-check').forEach(function (b) { b.closest('.vw-bestellung-wrap').classList.toggle('ist-gewaehlt', b.checked); });
    };
    document.addEventListener('change', function (e) { if (e.target.classList.contains('vw-bestell-check')) zaehleBestellungen(); });
    zaehleBestellungen();
  }
  var zielBestellung = window.location.hash && document.getElementById(window.location.hash.slice(1));
  if (zielBestellung && zielBestellung.classList.contains('vw-bestellung-wrap')) {
    var d = $('details', zielBestellung); if (d) d.open = true;
    zielBestellung.scrollIntoView({ block: 'start' });
  }

  /* ---------- Einkaufsliste: Händler auf- und zuklappen merken ---------- */
  $$('details[data-merken]').forEach(function (d) {
    var key = 'vw-' + d.getAttribute('data-merken');
    try { if (localStorage.getItem(key) === '0') d.open = false; } catch (err) {}
    d.addEventListener('toggle', function () { try { localStorage.setItem(key, d.open ? '1' : '0'); } catch (err) {} });
  });

  /* ---------- Aufgaben: Standort-Häkchen "alle" ---------- */
  $$('[data-alle-standorte]').forEach(function (box) {
    box.addEventListener('change', function () {
      $$('input[data-standort]', box.closest('form')).forEach(function (b) { b.checked = box.checked; });
    });
  });

  /* ---------- Nachrichten: zum Ende des Verlaufs springen ---------- */
  var verlauf = $('.vw-chat-verlauf');
  if (verlauf) verlauf.scrollTop = verlauf.scrollHeight;
})();
