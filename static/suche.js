/* Gemeinsame Suche für alle Listen, die ohne Neuladen filtern.
   Dieselben Regeln stecken in app.py (suche_falten / suche_passt) –
   Änderungen bitte an beiden Stellen.

   Ein Suchbegriff gilt als Treffer, wenn er
     - irgendwo im Text steckt (auch mitten im Wort: "mehl" -> Weizenmehl, "5" -> 550)
     - zu den Anfangsbuchstaben passt ("ws5" -> Weizenmehl Sonnengold 550)
     - oder ein Wort trotz Tippfehler meint ("vanile" -> Vanille, "sonnegold" -> Sonnengold)
   Mehrere Begriffe müssen alle passen, die Reihenfolge ist egal. */
(function () {
  'use strict';

  function falten(text) {
    return String(text == null ? '' : text).toLowerCase()
      .replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue').replace(/ß/g, 'ss')
      .replace(/é|è/g, 'e').replace(/á|à/g, 'a').replace(/ç/g, 'c').replace(/ñ/g, 'n')
      .replace(/(\d)[.,](\d)/g, '$1$2')
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
  }

  function ziel(text) {
    var worte = falten(text).split(' ').filter(Boolean);
    var initialen = '';
    for (var i = 0; i < worte.length; i++) initialen += worte[i].charAt(0);
    return { worte: worte, kompakt: worte.join(''), initialen: initialen };
  }

  /* Levenshtein, bricht ab sobald der Abstand zu groß wird */
  function abstand(a, b, maximum) {
    if (Math.abs(a.length - b.length) > maximum) return maximum + 1;
    if (a === b) return 0;
    var vorher = [], j, i;
    for (j = 0; j <= b.length; j++) vorher[j] = j;
    for (i = 1; i <= a.length; i++) {
      var aktuell = [i], bester = i;
      for (j = 1; j <= b.length; j++) {
        var kosten = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
        var wert = Math.min(vorher[j] + 1, aktuell[j - 1] + 1, vorher[j - 1] + kosten);
        aktuell[j] = wert;
        if (wert < bester) bester = wert;
      }
      if (bester > maximum) return maximum + 1;
      vorher = aktuell;
    }
    return vorher[b.length];
  }

  function hatZiffer(text) { return /\d/.test(text); }

  function tokenPasst(z, token) {
    if (!token) return true;
    if (z.kompakt.indexOf(token) !== -1) return true;
    if (token.length >= 2 && z.initialen.indexOf(token) !== -1) return true;
    if (token.length < 3 || hatZiffer(token)) return false;
    /* zwei Tippfehler erst bei langen Wörtern ("straciatela"),
       bei kurzen führt das sonst zu Unsinn ("frische" -> "Flasche") */
    var maximum = token.length >= 9 ? 2 : 1;
    var i;
    for (i = 0; i < z.worte.length; i++) {
      var wort = z.worte[i];
      if (wort.length < 3) continue;
      if (abstand(wort, token, maximum) <= maximum) return true;
      if (wort.length > token.length
        && abstand(wort.slice(0, token.length + maximum), token, maximum) <= maximum) return true;
    }
    /* Tippfehler in einem zusammengeschriebenen Suchbegriff
       ("weizemehlsonnengold" für "Weizenmehl Sonnengold").
       Erst ab 6 Zeichen und nur gleich lange Stücke – sonst findet
       "mehl" auch "Karamellsoße" und "kirsche" jedes "Dänische". */
    if (token.length >= 6) {
      var kompakt = z.kompakt, a = token.charAt(0), b = token.charAt(1), laenge = token.length;
      for (i = 0; i + laenge <= kompakt.length; i++) {
        var zeichen = kompakt.charAt(i);
        if (zeichen !== a && zeichen !== b) continue;
        /* hier nur ein Tippfehler erlaubt: mit zwei würde "kirsche"
           auch "Türkische" und "frische" auch "Flasche" finden */
        if (abstand(kompakt.substr(i, laenge), token, 1) <= 1) return true;
      }
    }
    return false;
  }

  function begriffe(anfrage) {
    return falten(anfrage).split(' ').filter(Boolean);
  }

  /* Ziel einmal pro Element berechnen und am Element merken –
     sonst wird bei jedem Tastendruck alles neu zerlegt. */
  function zielVon(element, attribut) {
    var roh = element.getAttribute(attribut || 'data-suche') || '';
    if (element.__opZielRoh !== roh) {
      element.__opZielRoh = roh;
      element.__opZiel = ziel(roh);
    }
    return element.__opZiel;
  }

  function passt(textOderZiel, anfrage) {
    var tokens = typeof anfrage === 'string' ? begriffe(anfrage) : anfrage;
    if (!tokens || !tokens.length) return true;
    var z = (textOderZiel && textOderZiel.kompakt !== undefined) ? textOderZiel : ziel(textOderZiel);
    for (var i = 0; i < tokens.length; i++) {
      if (!tokenPasst(z, tokens[i])) return false;
    }
    return true;
  }

  /* Vorschlagsliste unter dem Suchfeld, wie bei einer Suchmaschine.
     Zeigt die ersten Treffer, Pfeiltasten und Klick wählen aus.
     optionen: { feld, zeilen, attribut, titelWahl, unterWahl, maximal, beiAuswahl } */
  function vorschlag(optionen) {
    var feld = optionen.feld;
    if (!feld || feld.__opVorschlag) return null;
    feld.__opVorschlag = true;

    var huelle = document.createElement('span');
    huelle.className = 'op-suche-huelle';
    feld.parentNode.insertBefore(huelle, feld);
    huelle.appendChild(feld);

    var kasten = document.createElement('div');
    kasten.className = 'op-vorschlag';
    kasten.hidden = true;
    kasten.setAttribute('role', 'listbox');
    huelle.appendChild(kasten);

    feld.setAttribute('autocomplete', 'off');
    feld.setAttribute('role', 'combobox');
    feld.setAttribute('aria-expanded', 'false');
    feld.setAttribute('aria-autocomplete', 'list');

    var treffer = [], aktiv = -1, stumm = false;

    function textVon(zeile, wahl) {
      if (!wahl) return '';
      var e = zeile.querySelector(wahl);
      return e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : '';
    }

    function zu() {
      kasten.hidden = true;
      kasten.innerHTML = '';
      feld.setAttribute('aria-expanded', 'false');
      treffer = [];
      aktiv = -1;
    }

    function markieren() {
      Array.prototype.forEach.call(kasten.children, function (kind, i) {
        kind.classList.toggle('ist-aktiv', i === aktiv);
        kind.setAttribute('aria-selected', i === aktiv ? 'true' : 'false');
      });
    }

    function waehlen(i) {
      var zeile = treffer[i];
      if (!zeile) return;
      var titel = textVon(zeile, optionen.titelWahl || 'b') || textVon(zeile, 'td');
      zu();
      if (optionen.beiAuswahl) {
        optionen.beiAuswahl(zeile, titel, feld);
      } else {
        if (titel) {
          feld.value = titel;
          stumm = true;   // die Liste soll nach der Auswahl zubleiben
          feld.dispatchEvent(new Event('input', { bubbles: true }));
          stumm = false;
        }
        if (zeile.scrollIntoView) zeile.scrollIntoView({ block: 'center' });
        zeile.classList.add('op-gefunden');
        setTimeout(function () { zeile.classList.remove('op-gefunden'); }, 1600);
      }
    }

    function aufbauen() {
      if (stumm) return;
      var tokens = begriffe(feld.value);
      if (!tokens.length) { zu(); return; }
      var alle = Array.prototype.slice.call(
        typeof optionen.zeilen === 'function' ? optionen.zeilen() : optionen.zeilen
      );
      var gefunden = [];
      for (var i = 0; i < alle.length && gefunden.length < (optionen.maximal || 8); i++) {
        if (passt(zielVon(alle[i], optionen.attribut), tokens)) gefunden.push(alle[i]);
      }
      treffer = gefunden;
      aktiv = -1;
      if (!treffer.length) { zu(); return; }
      kasten.innerHTML = '';
      treffer.forEach(function (zeile, i) {
        var eintrag = document.createElement('button');
        eintrag.type = 'button';
        eintrag.className = 'op-vorschlag-eintrag';
        eintrag.setAttribute('role', 'option');
        var titel = textVon(zeile, optionen.titelWahl || 'b') || textVon(zeile, 'b')
          || textVon(zeile, 'td') || (zeile.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 70) || '…';
        var unter = textVon(zeile, optionen.unterWahl || 'small');
        eintrag.innerHTML = '<b></b>' + (unter ? '<small></small>' : '');
        eintrag.querySelector('b').textContent = titel;
        if (unter) eintrag.querySelector('small').textContent = unter;
        eintrag.addEventListener('mousedown', function (e) { e.preventDefault(); waehlen(i); });
        eintrag.addEventListener('mouseenter', function () { aktiv = i; markieren(); });
        kasten.appendChild(eintrag);
      });
      kasten.hidden = false;
      feld.setAttribute('aria-expanded', 'true');
    }

    feld.addEventListener('input', aufbauen);
    feld.addEventListener('focus', aufbauen);
    feld.addEventListener('blur', function () { setTimeout(zu, 120); });
    feld.addEventListener('keydown', function (e) {
      if (kasten.hidden || !treffer.length) {
        if (e.key === 'Escape') { feld.value = ''; feld.dispatchEvent(new Event('input', { bubbles: true })); }
        return;
      }
      if (e.key === 'ArrowDown') { e.preventDefault(); aktiv = (aktiv + 1) % treffer.length; markieren(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); aktiv = (aktiv <= 0 ? treffer.length : aktiv) - 1; markieren(); }
      else if (e.key === 'Enter') {
        if (aktiv >= 0) { e.preventDefault(); waehlen(aktiv); }
        else { zu(); }
      } else if (e.key === 'Escape') { e.preventDefault(); zu(); }
    });
    return { aufbauen: aufbauen, zu: zu };
  }

  /* Hängt eine Suche an eine Liste: filtert bei jedem Tastendruck.
     optionen: { feld, zeilen (Funktion oder NodeList), attribut,
                 gruppen, leer, zaehler, zusatz (Funktion -> bool) } */
  function verbinden(optionen) {
    var feld = optionen.feld;
    if (!feld) return function () {};
    function zeilenJetzt() {
      var z = optionen.zeilen;
      return Array.prototype.slice.call(typeof z === 'function' ? z() : z);
    }
    function filtern() {
      var tokens = begriffe(feld.value);
      var zeilen = zeilenJetzt();
      var sichtbar = 0;
      zeilen.forEach(function (zeile) {
        var ok = passt(zielVon(zeile, optionen.attribut), tokens)
          && (!optionen.zusatz || optionen.zusatz(zeile));
        zeile.hidden = !ok;
        if (ok) sichtbar += 1;
      });
      if (optionen.gruppen) {
        var wahl = optionen.zeilenWahl || '[data-suche]:not([hidden])';
        Array.prototype.forEach.call(
          typeof optionen.gruppen === 'function' ? optionen.gruppen() : optionen.gruppen,
          function (gruppe) {
            gruppe.hidden = tokens.length > 0 && !gruppe.querySelector(wahl);
            if (tokens.length && gruppe.tagName === 'DETAILS') gruppe.open = true;
          }
        );
      }
      if (optionen.leer) optionen.leer.hidden = sichtbar > 0 || !zeilen.length;
      if (optionen.zaehler) optionen.zaehler(sichtbar, zeilen.length, tokens.length > 0);
      /* Zwischenüberschriften (z. B. Datumszeilen) ohne sichtbare Zeile darunter ausblenden */
      if (optionen.ueberschriften) {
        var reihe = Array.prototype.slice.call(document.querySelectorAll(
          optionen.ueberschriften + ',' + (optionen.zeilenWahl || '[data-suche]')
        ));
        var offen = null;
        reihe.concat([null]).forEach(function (element) {
          var istKopf = element === null || element.matches(optionen.ueberschriften);
          if (istKopf) {
            if (offen) offen.kopf.hidden = tokens.length > 0 && !offen.treffer;
            offen = element ? { kopf: element, treffer: false } : null;
          } else if (offen && !element.hidden) {
            offen.treffer = true;
          }
        });
      }
    }
    feld.addEventListener('input', filtern);
    feld.addEventListener('search', filtern);
    if (optionen.vorschlaege !== false) {
      vorschlag({
        feld: feld,
        zeilen: optionen.zeilen,
        attribut: optionen.attribut,
        titelWahl: optionen.titelWahl,
        unterWahl: optionen.unterWahl,
        beiAuswahl: optionen.beiAuswahl
      });
    }
    if (feld.value) filtern();
    return filtern;
  }

  window.opSuche = {
    vorschlag: vorschlag,
    falten: falten,
    ziel: ziel,
    zielVon: zielVon,
    begriffe: begriffe,
    passt: passt,
    verbinden: verbinden
  };
})();
