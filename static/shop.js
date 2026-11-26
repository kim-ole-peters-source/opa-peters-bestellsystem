/* Bestellseite: Suche und Warengruppen filtern sofort, ohne Neuladen.
   Warenkorb, Zwischenspeicher und Absenden bleiben in app.js –
   dieses Skript liest nur die Mengen mit und passt die Anzeige an. */
(function () {
  'use strict';

  var shop = document.querySelector('.shop');
  if (!shop) return;

  var suche = document.getElementById('shopSuche');
  var chips = document.getElementById('shopChips');
  var leer = document.getElementById('shopLeer');
  var treffer = document.getElementById('shopTreffer');
  var leiste = document.getElementById('shopKorbleiste');
  var zaehler = document.getElementById('orderCount');
  var karten = Array.prototype.slice.call(document.querySelectorAll('.shop .product-card'));
  var abschnitte = Array.prototype.slice.call(document.querySelectorAll('.shop-abschnitt'));
  var gruppe = shop.getAttribute('data-start-gruppe') || '';

  /* Die tolerante Suche steckt in suche.js. Kommt die Datei nicht an
     (Netzaussetzer), filtert diese einfache Fassung weiter – lieber
     Teilstueck-Suche als eine Seite, auf der nichts mehr geht. */
  function begriffeVon(wert) {
    if (window.opSuche) return window.opSuche.begriffe(wert);
    return String(wert || '').toLowerCase().split(/\s+/).filter(Boolean);
  }
  function passtZu(element, begriffe) {
    if (window.opSuche) return window.opSuche.passt(window.opSuche.zielVon(element), begriffe);
    var text = (element.getAttribute('data-suche') || '').toLowerCase();
    return begriffe.every(function (b) { return text.indexOf(b) !== -1; });
  }

  function filtern() {
    var begriffe = begriffeVon(suche ? suche.value : '');
    var gefiltert = !!gruppe || begriffe.length > 0;
    var sichtbar = 0;

    karten.forEach(function (karte) {
      var gruppen = (karte.getAttribute('data-kategorien') || '').split('|');
      var passt = (!gruppe || gruppen.indexOf(gruppe) !== -1) && passtZu(karte, begriffe);
      karte.hidden = !passt;
      if (passt) sichtbar += 1;
    });

    abschnitte.forEach(function (abschnitt) {
      var hatTreffer = abschnitt.querySelector('.product-card:not([hidden])');
      abschnitt.hidden = !hatTreffer;
    });
    shop.classList.toggle('shop-gefiltert', gefiltert);

    if (leer) leer.hidden = sichtbar > 0;
    if (treffer) {
      treffer.textContent = gefiltert ? (sichtbar === 1 ? '1 Produkt' : sichtbar + ' Produkte') : '';
    }
  }

  /* Vorschlagsliste beim Tippen: Treffer antippen springt zur Karte */
  if (suche && window.opSuche && window.opSuche.vorschlag) {
    window.opSuche.vorschlag({
      feld: suche,
      zeilen: function () { return karten; },
      titelWahl: 'h3',
      unterWahl: '.pk-text p',
      beiAuswahl: function (karte, titel) {
        suche.value = titel;
        filtern();
        if (karte.scrollIntoView) karte.scrollIntoView({ block: 'center' });
        karte.classList.add('op-gefunden');
        setTimeout(function () { karte.classList.remove('op-gefunden'); }, 1600);
      }
    });
  }

  function gruppeWaehlen(name) {
    gruppe = name;
    if (!chips) return;
    Array.prototype.forEach.call(chips.querySelectorAll('.shop-chip'), function (chip) {
      chip.setAttribute('aria-pressed', chip.getAttribute('data-gruppe') === gruppe ? 'true' : 'false');
    });
  }

  /* Karten im Warenkorb hervorheben, Leiste nur zeigen, wenn etwas drin ist */
  function mengenAbgleichen() {
    var positionen = 0;
    karten.forEach(function (karte) {
      var id = karte.getAttribute('data-product-id');
      var feld = document.getElementById('qty_' + id);
      var menge = feld ? parseInt(feld.value || '0', 10) || 0 : 0;
      karte.classList.toggle('im-korb', menge > 0);
    });
    Array.prototype.forEach.call(document.querySelectorAll('form[action="/order"] input[type="hidden"][name^="qty_"]'), function (feld) {
      if ((parseInt(feld.value || '0', 10) || 0) > 0) positionen += 1;
    });
    if (leiste) leiste.setAttribute('data-leer', positionen ? '0' : '1');
    document.documentElement.classList.toggle('shop-hat-korb', positionen > 0);
  }

  if (suche) {
    suche.addEventListener('input', filtern);
    suche.form.addEventListener('submit', function (event) {
      event.preventDefault();
      suche.blur();
    });
  }

  if (chips) {
    chips.addEventListener('click', function (event) {
      var chip = event.target.closest('.shop-chip');
      if (!chip) return;
      var name = chip.getAttribute('data-gruppe') || '';
      gruppeWaehlen(gruppe === name ? '' : name);
      filtern();
      var oben = document.getElementById('shopProdukte');
      if (oben && window.scrollY > oben.offsetTop) window.scrollTo({ top: oben.offsetTop - 120, behavior: 'smooth' });
    });
  }

  /* app.js schreibt nach jeder Änderung die Positionszahl neu – darauf hören wir */
  if (zaehler && window.MutationObserver) {
    new MutationObserver(mengenAbgleichen).observe(zaehler, { childList: true, characterData: true, subtree: true });
  }
  document.addEventListener('click', function () { window.setTimeout(mengenAbgleichen, 0); });
  document.addEventListener('input', function () { window.setTimeout(mengenAbgleichen, 0); });

  gruppeWaehlen(gruppe);
  filtern();
  mengenAbgleichen();
})();
