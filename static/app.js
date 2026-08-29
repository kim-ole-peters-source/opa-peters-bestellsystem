(function () {
  if (window.Element && !Element.prototype.matches) {
    Element.prototype.matches = Element.prototype.msMatchesSelector || Element.prototype.webkitMatchesSelector;
  }
  if (window.Element && !Element.prototype.closest) {
    Element.prototype.closest = function (selector) {
      var node = this;
      while (node && node.nodeType === 1) {
        if (node.matches && node.matches(selector)) return node;
        node = node.parentElement || node.parentNode;
      }
      return null;
    };
  }
  if (window.NodeList && !NodeList.prototype.forEach) {
    NodeList.prototype.forEach = Array.prototype.forEach;
  }

  var nonPassiveEvent = false;
  try {
    var passiveTest = Object.defineProperty({}, 'passive', {
      get: function () {
        nonPassiveEvent = { passive: false };
        return false;
      }
    });
    window.addEventListener('testPassive', null, passiveTest);
    window.removeEventListener('testPassive', null, passiveTest);
  } catch (error) {
    nonPassiveEvent = false;
  }

  function toArray(list) {
    return Array.prototype.slice.call(list || []);
  }

  function triggerInputEvent(input) {
    if (!input) return;
    var event;
    if (typeof Event === 'function') {
      event = new Event('input', { bubbles: true });
    } else {
      event = document.createEvent('Event');
      event.initEvent('input', true, true);
    }
    input.dispatchEvent(event);
  }

  function repeatText(text, count) {
    var result = '';
    for (var index = 0; index < count; index += 1) result += text;
    return result;
  }

  function addLegacyTapListener(element, handler) {
    if (!element) return;
    var lastTouch = 0;
    element.addEventListener('touchend', function (event) {
      lastTouch = Date.now();
      handler(event);
    }, nonPassiveEvent);
    element.addEventListener('click', function (event) {
      if (Date.now() - lastTouch < 500) return;
      handler(event);
    });
  }

  function addClassName(element, className) {
    if (!element) return;
    if (element.className.indexOf(className) === -1) {
      element.className = (element.className + ' ' + className).replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
    }
  }

  function removeClassName(element, className) {
    if (!element) return;
    element.className = (' ' + element.className + ' ').replace(' ' + className + ' ', ' ').replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
  }

  function addImmediateTouchListener(element, handler) {
    if (!element) return;
    var lastRun = 0;
    element.addEventListener('touchstart', function (event) {
      lastRun = Date.now();
      handler(event);
    }, nonPassiveEvent);
    element.addEventListener('touchend', function (event) {
      if (Date.now() - lastRun < 700) return;
      lastRun = Date.now();
      handler(event);
    }, nonPassiveEvent);
    element.addEventListener('click', function (event) {
      if (Date.now() - lastRun < 700) return;
      handler(event);
    });
  }

  var serviceWorkerReady = null;
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      serviceWorkerReady = navigator.serviceWorker.register('/service-worker.js').then(function () {
        return navigator.serviceWorker.ready;
      }).catch(function () { return null; });
    });
  }

  function urlBase64ToUint8Array(base64String) {
    var padding = repeatText('=', (4 - base64String.length % 4) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var rawData = window.atob(base64);
    var outputArray = new Uint8Array(rawData.length);
    for (var i = 0; i < rawData.length; i += 1) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  function postJson(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(data || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok || payload.ok === false) {
          throw new Error(payload.error || 'Aktion fehlgeschlagen.');
        }
        return payload;
      });
    });
  }

  var pushControl = document.getElementById('pushControl');
  var pushToggle = document.getElementById('pushToggle');
  var pushStatus = document.getElementById('pushStatus');
  var pushConfig = null;
  var currentPushSubscription = null;

  function setPushText(text, enabled) {
    if (pushStatus) pushStatus.textContent = text;
    if (pushToggle) pushToggle.textContent = enabled ? 'Push deaktivieren' : 'Push aktivieren';
  }

  function refreshPushState() {
    if (!pushControl || !pushToggle) return;
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return;
    fetch('/push/config', { credentials: 'same-origin' }).then(function (response) {
      if (!response.ok) throw new Error('Keine Push-Konfiguration.');
      return response.json();
    }).then(function (config) {
      pushConfig = config;
      if (!config.enabled || !config.publicKey) {
        pushControl.hidden = false;
        pushToggle.disabled = true;
        setPushText('Push ist auf dem Server noch nicht eingerichtet.', false);
        return;
      }
      pushControl.hidden = false;
      return (serviceWorkerReady || navigator.serviceWorker.ready).then(function (registration) {
        if (!registration || !registration.pushManager) return null;
        return registration.pushManager.getSubscription();
      }).then(function (subscription) {
        currentPushSubscription = subscription;
        if (Notification.permission === 'denied') {
          pushToggle.disabled = true;
          setPushText('Push wurde im Browser blockiert.', false);
        } else if (subscription) {
          pushToggle.disabled = false;
          setPushText('Push auf diesem Gerät aktiv.', true);
        } else {
          pushToggle.disabled = false;
          setPushText('Push auf diesem Gerät aus.', false);
        }
      });
    }).catch(function () {});
  }

  if (pushToggle) {
    pushToggle.addEventListener('click', function () {
      if (!pushConfig || !pushConfig.publicKey) return;
      pushToggle.disabled = true;
      (serviceWorkerReady || navigator.serviceWorker.ready).then(function (registration) {
        if (currentPushSubscription) {
          var endpoint = currentPushSubscription.endpoint;
          return postJson('/push/unsubscribe', { endpoint: endpoint }).then(function () {
            return currentPushSubscription.unsubscribe().catch(function () {});
          }).then(function () {
            currentPushSubscription = null;
            setPushText('Push auf diesem Gerät aus.', false);
          });
        }
        return Notification.requestPermission().then(function (permission) {
          if (permission !== 'granted') {
            setPushText('Push wurde nicht erlaubt.', false);
            return null;
          }
          return registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(pushConfig.publicKey)
          }).then(function (subscription) {
            return postJson('/push/subscribe', { subscription: subscription.toJSON() }).then(function () {
              currentPushSubscription = subscription;
              setPushText('Push auf diesem Gerät aktiv.', true);
            });
          });
        });
      }).catch(function () {
        setPushText('Push konnte nicht geändert werden.', !!currentPushSubscription);
      }).then(function () {
        pushToggle.disabled = false;
      });
    });
  }
  window.addEventListener('load', refreshPushState);

  var deferredPrompt = null;
  var button = document.getElementById('installApp');
  var iosHelp = document.getElementById('iosInstallHelp');
  var iosClose = document.querySelector('.install-help-close');
  var isAppleMobile = /iphone|ipad|ipod/i.test(navigator.userAgent || '') || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var isStandalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || window.navigator.standalone;
  if (button && isAppleMobile && !isStandalone) {
    button.hidden = false;
  }
  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (button) button.hidden = false;
  });
  if (button) {
    button.addEventListener('click', function () {
      if (!deferredPrompt) {
        if (iosHelp) iosHelp.hidden = false;
        return;
      }
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then(function () {
        deferredPrompt = null;
        button.hidden = true;
      }).catch(function () {
        deferredPrompt = null;
        button.hidden = true;
      });
    });
  }
  if (iosClose && iosHelp) {
    iosClose.addEventListener('click', function () {
      iosHelp.hidden = true;
    });
    iosHelp.addEventListener('click', function (event) {
      if (event.target === iosHelp) iosHelp.hidden = true;
    });
  }

  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      var message = form.getAttribute('data-confirm') || 'Wirklich ausführen?';
      if (!window.confirm(message)) event.preventDefault();
    });
  });

  document.querySelectorAll('.password-toggle').forEach(function (toggle) {
    toggle.addEventListener('click', function () {
      var wrap = toggle.closest('.password-wrap');
      var input = wrap ? wrap.querySelector('input') : null;
      if (!input) return;
      var visible = input.type === 'text';
      input.type = visible ? 'password' : 'text';
      toggle.textContent = visible ? 'Anzeigen' : 'Verbergen';
    });
  });

  function createPinPad() {
    var overlay = document.createElement('div');
    overlay.className = 'pinpad-overlay';
    overlay.hidden = true;
    overlay.innerHTML = '<div class="pinpad-card">' +
      '<h2>PIN eingeben</h2>' +
      '<div class="pinpad-display" aria-live="polite"></div>' +
      '<div class="pinpad-grid">' +
      [1, 2, 3, 4, 5, 6, 7, 8, 9].map(function (number) {
        return '<button type="button" data-pin-digit="' + number + '">' + number + '</button>';
      }).join('') +
      '<button type="button" data-pin-action="delete">Löschen</button>' +
      '<button type="button" data-pin-digit="0">0</button>' +
      '<button type="button" class="primary" data-pin-action="done">OK</button>' +
      '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    return overlay;
  }

  var activePinInput = null;
  var pinpadOpenedAt = 0;
  var pinpad = document.querySelector('.pinpad-overlay') || createPinPad();
  var pinDisplay = pinpad.querySelector('.pinpad-display');

  function positionPinPad() {
    var viewport = window.visualViewport;
    if (viewport) {
      pinpad.style.left = viewport.offsetLeft + 'px';
      pinpad.style.top = viewport.offsetTop + 'px';
      pinpad.style.width = viewport.width + 'px';
      pinpad.style.height = viewport.height + 'px';
    } else {
      pinpad.style.left = '0px';
      pinpad.style.top = '0px';
      pinpad.style.width = '100vw';
      pinpad.style.height = '100vh';
    }
  }

  function updatePinDisplay() {
    if (!activePinInput || !pinDisplay) return;
    pinDisplay.textContent = activePinInput.value ? repeatText('•', activePinInput.value.length) : 'PIN';
  }

  function openPinPad(input) {
    activePinInput = input;
    input.blur();
    positionPinPad();
    updatePinDisplay();
    document.documentElement.classList.add('modal-open');
    pinpad.hidden = false;
    pinpadOpenedAt = Date.now();
  }

  function closePinPad() {
    pinpad.hidden = true;
    document.documentElement.classList.remove('modal-open');
    if (activePinInput) activePinInput.blur();
  }

  document.querySelectorAll('.numeric-pin').forEach(function (input) {
    input.readOnly = true;
    function preventNativeFocus(event) {
      event.preventDefault();
    }
    function interceptPinOpen(event) {
      event.preventDefault();
      openPinPad(input);
    }
    input.addEventListener('pointerdown', preventNativeFocus);
    input.addEventListener('mousedown', preventNativeFocus);
    input.addEventListener('touchstart', preventNativeFocus, nonPassiveEvent);
    input.addEventListener('touchend', interceptPinOpen, nonPassiveEvent);
    input.addEventListener('click', interceptPinOpen);
    input.addEventListener('focus', function () {
      openPinPad(input);
    });
  });

  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', function () {
      if (!pinpad.hidden) positionPinPad();
    });
    window.visualViewport.addEventListener('scroll', function () {
      if (!pinpad.hidden) positionPinPad();
    });
  }

  pinpad.addEventListener('click', function (event) {
    var digitButton = event.target.closest('[data-pin-digit]');
    var actionButton = event.target.closest('[data-pin-action]');
    if (digitButton && activePinInput) {
      activePinInput.value += digitButton.getAttribute('data-pin-digit');
      triggerInputEvent(activePinInput);
      updatePinDisplay();
    }
    if (actionButton && activePinInput) {
      var action = actionButton.getAttribute('data-pin-action');
      if (action === 'delete') {
        activePinInput.value = activePinInput.value.slice(0, -1);
        triggerInputEvent(activePinInput);
        updatePinDisplay();
      }
      if (action === 'done') {
        closePinPad();
      }
    }
    if (event.target === pinpad && Date.now() - pinpadOpenedAt > 250) closePinPad();
  });

  var orderForm = document.querySelector('form[action="/order"]');
  var orderCount = document.getElementById('orderCount');
  var reviewOrder = document.getElementById('reviewOrder');
  var cartReview = document.getElementById('cartReview');
  var cartItems = document.getElementById('cartItems');
  var cartCancel = document.getElementById('cartCancel');
  var cartSubmit = document.getElementById('cartSubmit');
  var locationInput = orderForm ? orderForm.querySelector('input[name="location"]') : null;
  var cartStorageKey = locationInput ? 'opaPetersCart:' + locationInput.value : 'opaPetersCart';
  var cartStorageBackupKey = locationInput ? 'opaPetersCartBackup:' + locationInput.value : 'opaPetersCartBackup';
  var restoringCart = false;
  var waitingForServerCart = false;
  var cartChangedWhileLoading = false;
  var serverSaveTimer = null;
  var orderWasSubmitted = false;

  function clampQty(value) {
    var number = parseInt(value || '0', 10);
    if (isNaN(number) || number < 0) number = 0;
    return Math.min(number, 9999);
  }

  function getHiddenQty(productId) {
    return orderForm ? orderForm.querySelector('input[type="hidden"][name="qty_' + productId + '"]') : null;
  }

  function normalizeCartState(rawState) {
    var state = rawState || {};
    var rawItems = state.items || state;
    var items = {};
    var details = state.details || {};
    Object.keys(rawItems || {}).forEach(function (productId) {
      var qty = clampQty(rawItems[productId]);
      if (qty > 0) items[productId] = qty;
    });
    return {
      items: items,
      details: {
        ordered_by: String(details.ordered_by || ''),
        note: String(details.note || '')
      },
      updatedAt: state.updatedAt || Date.now()
    };
  }

  function cartStateHasContent(state) {
    state = normalizeCartState(state);
    return !!(Object.keys(state.items).length || state.details.ordered_by || state.details.note);
  }

  function currentCartDetails() {
    return {
      ordered_by: orderForm && orderForm.elements.ordered_by ? orderForm.elements.ordered_by.value : '',
      note: orderForm && orderForm.elements.note ? orderForm.elements.note.value : ''
    };
  }

  function currentCartState() {
    var items = {};
    if (orderForm) {
      orderForm.querySelectorAll('input[type="hidden"][name^="qty_"]').forEach(function (input) {
        var productId = input.getAttribute('data-product-id');
        var qty = clampQty(input.value);
        if (productId && qty > 0) items[productId] = qty;
      });
    }
    return normalizeCartState({
      items: items,
      details: currentCartDetails(),
      updatedAt: Date.now()
    });
  }

  function applyCartDetails(details) {
    details = details || {};
    if (orderForm && orderForm.elements.ordered_by) orderForm.elements.ordered_by.value = details.ordered_by || '';
    if (orderForm && orderForm.elements.note) orderForm.elements.note.value = details.note || '';
  }

  function applyCartState(rawState) {
    if (!orderForm) return;
    var state = normalizeCartState(rawState);
    restoringCart = true;
    orderForm.querySelectorAll('input[type="hidden"][name^="qty_"]').forEach(function (input) {
      input.value = '0';
    });
    orderForm.querySelectorAll('.qty-display[data-product-id]').forEach(function (input) {
      input.value = '0';
    });
    Object.keys(state.items).forEach(function (productId) {
      setProductQty(productId, state.items[productId]);
    });
    applyCartDetails(state.details);
    restoringCart = false;
    updateOrderCount();
  }

  function readStoredCart() {
    try {
      var raw = window.localStorage.getItem(cartStorageKey) || window.localStorage.getItem(cartStorageBackupKey) || '{}';
      return normalizeCartState(JSON.parse(raw) || {});
    } catch (error) {
      return normalizeCartState({});
    }
  }

  function persistCartLocally(state) {
    state = normalizeCartState(state);
    try {
      if (cartStateHasContent(state)) {
        var serialized = JSON.stringify(state);
        window.localStorage.setItem(cartStorageKey, serialized);
        window.localStorage.setItem(cartStorageBackupKey, serialized);
      } else {
        window.localStorage.removeItem(cartStorageKey);
        window.localStorage.removeItem(cartStorageBackupKey);
      }
    } catch (error) {}
  }

  function sendCartToServer(state, synchronous) {
    if (!orderForm || orderWasSubmitted) return;
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/cart-draft', !synchronous);
      xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
      xhr.send(JSON.stringify({ state: normalizeCartState(state) }));
    } catch (error) {}
  }

  function queueServerCartSave(state) {
    if (orderWasSubmitted) return;
    if (serverSaveTimer) window.clearTimeout(serverSaveTimer);
    serverSaveTimer = window.setTimeout(function () {
      serverSaveTimer = null;
      sendCartToServer(state, false);
    }, 350);
  }

  function writeStoredCart(options) {
    if (!orderForm || restoringCart || orderWasSubmitted) return;
    var state = currentCartState();
    persistCartLocally(state);
    if (waitingForServerCart) cartChangedWhileLoading = true;
    if (!options || options.server !== false) queueServerCartSave(state);
  }

  function clearStoredCart() {
    if (serverSaveTimer) window.clearTimeout(serverSaveTimer);
    try {
      window.localStorage.removeItem(cartStorageKey);
      window.localStorage.removeItem(cartStorageBackupKey);
    } catch (error) {}
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/cart-draft', false);
      xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
      xhr.send(JSON.stringify({ action: 'clear' }));
    } catch (error) {}
  }

  function markOrderSubmittedAndClearCart() {
    orderWasSubmitted = true;
    if (serverSaveTimer) window.clearTimeout(serverSaveTimer);
    serverSaveTimer = null;
    try {
      window.localStorage.removeItem(cartStorageKey);
      window.localStorage.removeItem(cartStorageBackupKey);
    } catch (error) {}
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/cart-draft', false);
      xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
      xhr.send(JSON.stringify({ action: 'clear' }));
    } catch (error) {}
  }

  function restoreStoredCart() {
    if (!orderForm) return;
    applyCartState(readStoredCart());
    updateOrderCount();
  }

  function loadServerCart() {
    if (!orderForm) return;
    waitingForServerCart = true;
    try {
      var localState = readStoredCart();
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/cart-draft', true);
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        waitingForServerCart = false;
        if (xhr.status !== 200) {
          if (cartStateHasContent(localState)) sendCartToServer(localState, false);
          return;
        }
        try {
          var response = JSON.parse(xhr.responseText || '{}');
          var serverState = normalizeCartState(response.state || {});
          if (cartChangedWhileLoading) {
            sendCartToServer(currentCartState(), false);
          } else if (cartStateHasContent(serverState)) {
            applyCartState(serverState);
            persistCartLocally(serverState);
          } else if (cartStateHasContent(localState)) {
            sendCartToServer(localState, false);
          }
          cartChangedWhileLoading = false;
          renderCart();
        } catch (error) {}
      };
      xhr.send();
    } catch (error) {
      waitingForServerCart = false;
    }
  }

  function setProductQty(productId, value) {
    if (!orderForm) return;
    var qty = clampQty(value);
    var hidden = getHiddenQty(productId);
    if (hidden) hidden.value = String(qty);
    orderForm.querySelectorAll('.qty-display[data-product-id="' + productId + '"]').forEach(function (input) {
      input.value = String(qty);
    });
    updateOrderCount();
    writeStoredCart();
  }

  function selectedItems() {
    if (!orderForm) return [];
    return toArray(orderForm.querySelectorAll('input[type="hidden"][name^="qty_"]')).map(function (input) {
      return {
        id: input.getAttribute('data-product-id'),
        name: input.getAttribute('data-product-name') || 'Produkt',
        packageSize: input.getAttribute('data-product-package') || '',
        qty: clampQty(input.value)
      };
    }).filter(function (item) {
      return item.qty > 0;
    });
  }

  function updateOrderCount() {
    if (!orderForm || !orderCount) return;
    var selected = selectedItems().length;
    orderCount.textContent = selected === 1 ? '1 Position' : selected + ' Positionen';
  }

  function renderCart() {
    if (!cartItems) return;
    var items = selectedItems();
    if (!items.length) {
      cartItems.innerHTML = '<p class="error">Bitte wähle mindestens ein Produkt aus.</p>';
      return;
    }
    cartItems.innerHTML = items.map(function (item) {
      return '<div class="cart-line" data-product-id="' + item.id + '">' +
        '<div><strong>' + item.name + '</strong><span>' + item.packageSize + '</span></div>' +
        '<div class="quantity-control small">' +
        '<button type="button" class="qty-minus" data-product-id="' + item.id + '">−</button>' +
        '<input class="qty-display" type="number" data-product-id="' + item.id + '" min="0" step="1" value="' + item.qty + '" inputmode="numeric">' +
        '<button type="button" class="qty-plus" data-product-id="' + item.id + '">+</button>' +
        '</div>' +
        '<button type="button" class="cart-remove" data-product-id="' + item.id + '">Entfernen</button>' +
      '</div>';
    }).join('');
  }

  if (orderForm && orderCount) {
    function handleQuantityAction(event) {
      var target = event.target;
      var plus = target && target.closest ? target.closest('.qty-plus') : null;
      var minus = target && target.closest ? target.closest('.qty-minus') : null;
      var remove = target && target.closest ? target.closest('.cart-remove') : null;
      if (plus || minus || remove) {
        if (event) event.preventDefault();
        var productId = (plus || minus || remove).getAttribute('data-product-id');
        var current = getHiddenQty(productId);
        var value = current ? clampQty(current.value) : 0;
        if (plus) setProductQty(productId, value + 1);
        if (minus) setProductQty(productId, value - 1);
        if (remove) setProductQty(productId, 0);
        renderCart();
      }
    }
    addLegacyTapListener(orderForm, handleQuantityAction);
    document.addEventListener('click', function (event) {
      var button = event.target && event.target.closest ? event.target.closest('.min-stock-cart-button') : null;
      if (!button) return;
      event.preventDefault();
      var productId = button.getAttribute('data-product-id');
      var minQty = clampQty(button.getAttribute('data-min-qty') || '1');
      if (!productId || !getHiddenQty(productId)) return;
      setProductQty(productId, minQty || 1);
      renderCart();
      button.textContent = 'Im Warenkorb';
      window.setTimeout(function () {
        button.textContent = 'In den Warenkorb';
      }, 1200);
    });
    orderForm.addEventListener('input', function (event) {
      if (event.target && event.target.matches && event.target.matches('.qty-display[data-product-id]')) {
        setProductQty(event.target.getAttribute('data-product-id'), event.target.value);
        renderCart();
        return;
      }
      updateOrderCount();
      writeStoredCart();
    });
    orderForm.addEventListener('change', function (event) {
      if (event.target && event.target.matches && event.target.matches('.qty-display[data-product-id]')) {
        setProductQty(event.target.getAttribute('data-product-id'), event.target.value);
        renderCart();
      }
    });
    function openCartReview(event) {
      if (event) event.preventDefault();
      if (!cartReview) return;
      renderCart();
      cartReview.hidden = false;
      addClassName(cartReview, 'is-open');
      document.documentElement.classList.add('modal-open');
    }
    function closeCartReview() {
      if (!cartReview) return;
      writeStoredCart();
      removeClassName(cartReview, 'is-open');
      cartReview.hidden = true;
      document.documentElement.classList.remove('modal-open');
    }
    if (reviewOrder && cartReview) {
      addImmediateTouchListener(reviewOrder, openCartReview);
    }
    if (cartCancel && cartReview) {
      addLegacyTapListener(cartCancel, closeCartReview);
    }
    if (cartSubmit) {
      addLegacyTapListener(cartSubmit, function (event) {
        if (event) event.preventDefault();
        if (!selectedItems().length) {
          renderCart();
          return;
        }
        if (orderForm.checkValidity && !orderForm.checkValidity()) {
          if (orderForm.reportValidity) orderForm.reportValidity();
          return;
        }
        document.documentElement.classList.remove('modal-open');
        markOrderSubmittedAndClearCart();
        if (orderForm.requestSubmit) {
          orderForm.requestSubmit();
        } else {
          orderForm.submit();
        }
      });
    }
    toArray(document.querySelectorAll('a[href="/logout"]')).forEach(function (link) {
      addLegacyTapListener(link, function (event) {
        if (event) event.preventDefault();
        writeStoredCart();
        sendCartToServer(currentCartState(), true);
        window.location.href = link.getAttribute('href');
      });
    });
    window.addEventListener('pagehide', function () {
      if (orderWasSubmitted) return;
      writeStoredCart({ server: false });
      sendCartToServer(currentCartState(), true);
    });
    window.addEventListener('beforeunload', function () {
      if (orderWasSubmitted) return;
      writeStoredCart({ server: false });
      sendCartToServer(currentCartState(), true);
    });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden && !orderWasSubmitted) {
        writeStoredCart({ server: false });
        sendCartToServer(currentCartState(), true);
      }
    });
    restoreStoredCart();
    loadServerCart();
    updateOrderCount();
  }

  var imageMode = document.getElementById('orderImageMode');
  var imageFields = document.querySelectorAll('.order-image-field');
  function updateImageFields() {
    if (!imageMode) return;
    imageFields.forEach(function (field) {
      var active = field.getAttribute('data-image-field') === imageMode.value;
      field.hidden = !active;
      var input = field.querySelector('input[type="file"]');
      if (input) {
        input.disabled = !active;
        if (!active) input.value = '';
      }
    });
  }
  if (imageMode) {
    imageMode.addEventListener('change', updateImageFields);
    updateImageFields();
  }

  var selectAllProducts = document.getElementById('selectAllProducts');
  var bulkChecks = Array.prototype.slice.call(document.querySelectorAll('.bulk-product-check'));
  var bulkSelectedCount = document.getElementById('bulkSelectedCount');
  function updateBulkRowState(box) {
    var row = box && box.closest ? box.closest('.product-row') : null;
    if (!row) return;
    if (box.checked) {
      addClassName(row, 'bulk-selected');
    } else {
      removeClassName(row, 'bulk-selected');
    }
  }
  function updateBulkSelectionState() {
    if (!bulkChecks.length) return;
    var selected = bulkChecks.filter(function (item) { return item.checked; }).length;
    bulkChecks.forEach(updateBulkRowState);
    if (selectAllProducts) {
      selectAllProducts.checked = selected === bulkChecks.length;
      selectAllProducts.indeterminate = selected > 0 && selected < bulkChecks.length;
    }
    if (bulkSelectedCount) {
      bulkSelectedCount.textContent = selected + (selected === 1 ? ' Produkt ausgewählt' : ' Produkte ausgewählt');
    }
  }
  if (selectAllProducts && bulkChecks.length) {
    selectAllProducts.addEventListener('change', function () {
      bulkChecks.forEach(function (box) {
        if (!box.closest || !box.closest('tr[hidden]')) box.checked = selectAllProducts.checked;
      });
      updateBulkSelectionState();
    });
    bulkChecks.forEach(function (box) {
      box.addEventListener('change', updateBulkSelectionState);
    });
    updateBulkSelectionState();
  }

  var bulkCategorySearch = document.getElementById('bulkCategorySearch');
  var bulkCategoryButtons = Array.prototype.slice.call(document.querySelectorAll('.bulk-category-select'));
  function productRowMatchesCategory(row, categoryKey) {
    var keys = (row.getAttribute('data-category-keys') || '').split(/\s+/);
    return keys.indexOf(categoryKey) !== -1;
  }
  function filterBulkCategoryButtons() {
    var query = bulkCategorySearch ? bulkCategorySearch.value.toLowerCase() : '';
    bulkCategoryButtons.forEach(function (button) {
      button.hidden = query && button.textContent.toLowerCase().indexOf(query) === -1;
    });
  }
  if (bulkCategorySearch) {
    bulkCategorySearch.addEventListener('input', filterBulkCategoryButtons);
  }
  bulkCategoryButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      var categoryKey = button.getAttribute('data-category-key') || '';
      bulkChecks.forEach(function (box) {
        var row = box.closest ? box.closest('.product-row') : null;
        if (row && productRowMatchesCategory(row, categoryKey) && !row.hidden) {
          box.checked = true;
        }
      });
      updateBulkSelectionState();
    });
  });

  var bulkAddRows = document.getElementById('bulkAddProductRows');
  var bulkAddCount = document.getElementById('bulkAddProductCount');
  var bulkAddTemplate = document.getElementById('bulkAddProductTemplate');
  var addBulkProductRow = document.getElementById('addBulkProductRow');
  if (bulkAddRows && bulkAddCount && bulkAddTemplate && addBulkProductRow) {
    addBulkProductRow.addEventListener('click', function () {
      var nextIndex = parseInt(bulkAddCount.value, 10) || bulkAddRows.querySelectorAll('[data-bulk-add-row]').length;
      var wrapper = document.createElement('div');
      wrapper.innerHTML = bulkAddTemplate.innerHTML.split('__INDEX__').join(String(nextIndex));
      while (wrapper.firstElementChild) {
        bulkAddRows.appendChild(wrapper.firstElementChild);
      }
      bulkAddCount.value = String(nextIndex + 1);
    });
    bulkAddRows.addEventListener('click', function (event) {
      var button = event.target.closest ? event.target.closest('.bulk-add-remove') : null;
      if (!button) return;
      var row = button.closest ? button.closest('[data-bulk-add-row]') : null;
      if (row && row.parentNode) row.parentNode.removeChild(row);
    });
  }

  var selectAllOrders = document.getElementById('selectAllOrders');
  var orderChecks = Array.prototype.slice.call(document.querySelectorAll('.combine-order-check'));
  var ordersSelectedCount = document.getElementById('ordersSelectedCount');
  function updateOrderSelectionState() {
    if (!orderChecks.length) return;
    var selected = orderChecks.filter(function (item) { return item.checked; }).length;
    if (selectAllOrders) {
      selectAllOrders.checked = selected === orderChecks.length;
      selectAllOrders.indeterminate = selected > 0 && selected < orderChecks.length;
    }
    if (ordersSelectedCount) {
      ordersSelectedCount.textContent = selected + (selected === 1 ? ' Bestellung ausgewählt' : ' Bestellungen ausgewählt');
    }
  }
  if (selectAllOrders && orderChecks.length) {
    selectAllOrders.addEventListener('change', function () {
      orderChecks.forEach(function (box) { box.checked = selectAllOrders.checked; });
      updateOrderSelectionState();
    });
    orderChecks.forEach(function (box) {
      box.addEventListener('change', updateOrderSelectionState);
    });
    updateOrderSelectionState();
  }

  var visibilityCategoryToggles = Array.prototype.slice.call(document.querySelectorAll('.visibility-category-toggle'));
  var visibilityProductChecks = Array.prototype.slice.call(document.querySelectorAll('.visibility-product-check'));
  function updateVisibilityCategoryStates() {
    visibilityCategoryToggles.forEach(function (toggle) {
      var category = toggle.getAttribute('data-category') || '';
      var matching = visibilityProductChecks.filter(function (box) {
        return (box.getAttribute('data-category') || '') === category;
      });
      var selected = matching.filter(function (box) { return box.checked; }).length;
      toggle.checked = matching.length > 0 && selected === matching.length;
      toggle.indeterminate = selected > 0 && selected < matching.length;
    });
  }
  if (visibilityCategoryToggles.length && visibilityProductChecks.length) {
    visibilityCategoryToggles.forEach(function (toggle) {
      toggle.addEventListener('change', function () {
        var category = toggle.getAttribute('data-category') || '';
        visibilityProductChecks.forEach(function (box) {
          if ((box.getAttribute('data-category') || '') === category) {
            box.checked = toggle.checked;
          }
        });
        updateVisibilityCategoryStates();
      });
    });
    visibilityProductChecks.forEach(function (box) {
      box.addEventListener('change', updateVisibilityCategoryStates);
    });
    updateVisibilityCategoryStates();
  }

  var bulkTimeRows = document.getElementById('bulkTimeRows');
  var addBulkTimeRow = document.getElementById('addBulkTimeRow');
  var bulkTimeShiftCount = document.getElementById('bulkTimeShiftCount');
  function setBulkTimeCount(value) {
    if (bulkTimeShiftCount) bulkTimeShiftCount.value = String(value);
  }
  if (bulkTimeRows && addBulkTimeRow && bulkTimeShiftCount) {
    var nextBulkTimeIndex = parseInt(bulkTimeShiftCount.value, 10) || bulkTimeRows.querySelectorAll('[data-bulk-time-row]').length;
    addBulkTimeRow.addEventListener('click', function () {
      var template = bulkTimeRows.getAttribute('data-template') || '';
      if (!template) return;
      var wrapper = document.createElement('div');
      wrapper.innerHTML = template.split('__INDEX__').join(String(nextBulkTimeIndex));
      while (wrapper.firstElementChild) {
        bulkTimeRows.appendChild(wrapper.firstElementChild);
      }
      nextBulkTimeIndex += 1;
      setBulkTimeCount(nextBulkTimeIndex);
    });
    bulkTimeRows.addEventListener('click', function (event) {
      var button = event.target.closest ? event.target.closest('.bulk-time-remove') : null;
      if (!button) return;
      var row = button.closest ? button.closest('[data-bulk-time-row]') : null;
      if (row && row.parentNode) row.parentNode.removeChild(row);
    });
  }

  var minStockToggles = Array.prototype.slice.call(document.querySelectorAll('.min-stock-toggle'));
  function updateMinStockPanel(toggle) {
    var targetId = toggle.getAttribute('data-target');
    var target = targetId ? document.getElementById(targetId) : null;
    if (target) target.hidden = !toggle.checked;
  }
  minStockToggles.forEach(function (toggle) {
    toggle.addEventListener('change', function () {
      updateMinStockPanel(toggle);
    });
    updateMinStockPanel(toggle);
  });

  var addMinStockButtons = Array.prototype.slice.call(document.querySelectorAll('.add-min-stock-row'));
  addMinStockButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      var rows = document.getElementById(button.getAttribute('data-target') || '');
      if (!rows) return;
      var template = document.getElementById(rows.getAttribute('data-template-id') || '');
      var countInput = document.getElementById(rows.getAttribute('data-count-id') || '');
      if (!template || !countInput) return;
      var nextIndex = parseInt(countInput.value, 10) || rows.querySelectorAll('[data-min-stock-row]').length;
      var wrapper = document.createElement('div');
      wrapper.innerHTML = template.innerHTML.split('__INDEX__').join(String(nextIndex));
      while (wrapper.firstElementChild) {
        rows.appendChild(wrapper.firstElementChild);
      }
      countInput.value = String(nextIndex + 1);
    });
  });
  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.min-stock-remove') : null;
    if (!button) return;
    var row = button.closest ? button.closest('[data-min-stock-row]') : null;
    if (row && row.parentNode) row.parentNode.removeChild(row);
  });

  var addCockpitButtons = Array.prototype.slice.call(document.querySelectorAll('.add-cockpit-row'));
  addCockpitButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      var rows = document.getElementById(button.getAttribute('data-target') || '');
      if (!rows) return;
      var template = document.getElementById(rows.getAttribute('data-template-id') || '');
      var countInput = document.getElementById(rows.getAttribute('data-count-id') || '');
      if (!template || !countInput) return;
      var nextIndex = parseInt(countInput.value, 10) || rows.children.length;
      var wrapper = document.createElement('div');
      wrapper.innerHTML = template.innerHTML.split('__INDEX__').join(String(nextIndex));
      while (wrapper.firstElementChild) {
        rows.appendChild(wrapper.firstElementChild);
      }
      countInput.value = String(nextIndex + 1);
    });
  });
  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.cockpit-row-remove') : null;
    if (!button) return;
    var row = button.closest ? button.closest('.cockpit-admin-row') : null;
    if (row && row.parentNode) row.parentNode.removeChild(row);
  });

  var locationsOpenLocation = document.getElementById('locationsOpenLocation');
  if (locationsOpenLocation) {
    document.addEventListener('click', function (event) {
      var submitter = event.target.closest ? event.target.closest('button[type="submit"], input[type="submit"]') : null;
      if (!submitter) return;
      if (submitter.classList && submitter.classList.contains('location-delete-button')) {
        if (!window.confirm('Diesen Standort wirklich löschen?')) {
          event.preventDefault();
          return;
        }
      }
      var panel = submitter.closest ? submitter.closest('.location-panel[data-location-id]') : null;
      if (panel) locationsOpenLocation.value = panel.getAttribute('data-location-id') || '';
    });
    var locationsForm = locationsOpenLocation.closest('form');
    if (locationsForm) {
      locationsForm.addEventListener('submit', function (event) {
        var checkedRemove = locationsForm.querySelector('input[name^="location_remove_"]:checked');
        if (checkedRemove && !window.confirm('Markierte Standorte wirklich löschen?')) {
          event.preventDefault();
        }
      });
    }
  }
  var cockpitContentOpenLocation = document.getElementById('cockpitContentOpenLocation');
  if (cockpitContentOpenLocation) {
    document.addEventListener('click', function (event) {
      var submitter = event.target.closest ? event.target.closest('button[type="submit"], input[type="submit"]') : null;
      if (!submitter) return;
      var panel = submitter.closest ? submitter.closest('.cockpit-content-location[data-location-id]') : null;
      if (panel) cockpitContentOpenLocation.value = panel.getAttribute('data-location-id') || '';
    });
    var cockpitContentForm = cockpitContentOpenLocation.closest('form');
    if (cockpitContentForm) {
      cockpitContentForm.addEventListener('submit', function () {
        if (cockpitContentOpenLocation.value) return;
        var openPanel = cockpitContentForm.querySelector('.cockpit-content-location[data-location-id][open]');
        if (openPanel) cockpitContentOpenLocation.value = openPanel.getAttribute('data-location-id') || '';
      });
    }
  }
  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.activate-template-task') : null;
    if (!button) return;
    var targetId = button.getAttribute('data-target') || '';
    var checkbox = targetId ? document.getElementById(targetId) : null;
    if (checkbox) {
      checkbox.checked = true;
      checkbox.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  });

  var taskModal = document.getElementById('taskCompleteModal');
  var taskPersonSelect = document.getElementById('taskCompletePerson');
  var taskConfirm = document.getElementById('taskCompleteConfirm');
  var taskCancel = document.getElementById('taskCompleteCancel');
  var pendingTaskForm = null;
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || !form.classList || !form.classList.contains('cockpit-task-form')) return;
    var stateInput = form.querySelector('input[name="state"]');
    if (!stateInput || stateInput.value !== 'done') return;
    if (!taskModal || !taskPersonSelect || !taskConfirm) return;
    event.preventDefault();
    pendingTaskForm = form;
    if (taskPersonSelect.options.length) taskPersonSelect.selectedIndex = 0;
    taskModal.hidden = false;
    taskPersonSelect.focus();
  });
  if (taskConfirm) {
    taskConfirm.addEventListener('click', function () {
      if (!pendingTaskForm || !taskPersonSelect || !taskPersonSelect.value) return;
      var input = pendingTaskForm.querySelector('input[name="completed_by"]');
      if (input) input.value = taskPersonSelect.value;
      taskModal.hidden = true;
      pendingTaskForm.submit();
      pendingTaskForm = null;
    });
  }
  if (taskCancel) {
    taskCancel.addEventListener('click', function () {
      taskModal.hidden = true;
      pendingTaskForm = null;
    });
  }

  var productionStartModal = document.getElementById('productionStartModal');
  var productionStartPerson = document.getElementById('productionStartPerson');
  var productionStartConfirm = document.getElementById('productionStartConfirm');
  var productionStartCancel = document.getElementById('productionStartCancel');
  var pendingProductionStartForm = null;
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || !form.classList || !form.classList.contains('production-start-form')) return;
    if (!productionStartModal || !productionStartPerson || !productionStartConfirm) return;
    event.preventDefault();
    pendingProductionStartForm = form;
    productionStartPerson.value = '';
    productionStartModal.hidden = false;
    productionStartPerson.focus();
  });
  if (productionStartConfirm) {
    productionStartConfirm.addEventListener('click', function () {
      if (!pendingProductionStartForm || !productionStartPerson || !productionStartPerson.value.trim()) return;
      var input = pendingProductionStartForm.querySelector('input[name="completed_by"]');
      if (input) input.value = productionStartPerson.value.trim();
      productionStartModal.hidden = true;
      pendingProductionStartForm.submit();
      pendingProductionStartForm = null;
    });
  }
  if (productionStartCancel) {
    productionStartCancel.addEventListener('click', function () {
      productionStartModal.hidden = true;
      pendingProductionStartForm = null;
    });
  }

})();
