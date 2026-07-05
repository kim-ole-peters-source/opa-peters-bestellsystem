(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service-worker.js').catch(function () {});
    });
  }

  var deferredPrompt = null;
  var button = document.getElementById('installApp');
  var iosHelp = document.getElementById('iosInstallHelp');
  var iosClose = document.querySelector('.install-help-close');
  var isAppleMobile = /iphone|ipad|ipod/i.test(navigator.userAgent || '') || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
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
      deferredPrompt.userChoice.finally(function () {
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
  var pinpad = document.querySelector('.pinpad-overlay') || createPinPad();
  var pinDisplay = pinpad.querySelector('.pinpad-display');

  function updatePinDisplay() {
    if (!activePinInput || !pinDisplay) return;
    pinDisplay.textContent = activePinInput.value ? '•'.repeat(activePinInput.value.length) : 'PIN';
  }

  function openPinPad(input) {
    activePinInput = input;
    updatePinDisplay();
    pinpad.hidden = false;
  }

  document.querySelectorAll('.numeric-pin').forEach(function (input) {
    input.readOnly = true;
    input.addEventListener('click', function () { openPinPad(input); });
    input.addEventListener('touchend', function (event) {
      event.preventDefault();
      openPinPad(input);
    }, { passive: false });
  });

  pinpad.addEventListener('click', function (event) {
    var digitButton = event.target.closest('[data-pin-digit]');
    var actionButton = event.target.closest('[data-pin-action]');
    if (digitButton && activePinInput) {
      activePinInput.value += digitButton.getAttribute('data-pin-digit');
      activePinInput.dispatchEvent(new Event('input', { bubbles: true }));
      updatePinDisplay();
    }
    if (actionButton && activePinInput) {
      var action = actionButton.getAttribute('data-pin-action');
      if (action === 'delete') {
        activePinInput.value = activePinInput.value.slice(0, -1);
        activePinInput.dispatchEvent(new Event('input', { bubbles: true }));
        updatePinDisplay();
      }
      if (action === 'done') {
        pinpad.hidden = true;
        activePinInput.blur();
      }
    }
    if (event.target === pinpad) pinpad.hidden = true;
  });

  var orderForm = document.querySelector('form[action="/order"]');
  var orderCount = document.getElementById('orderCount');
  var reviewOrder = document.getElementById('reviewOrder');
  var cartReview = document.getElementById('cartReview');
  var cartItems = document.getElementById('cartItems');
  var cartCancel = document.getElementById('cartCancel');
  var cartSubmit = document.getElementById('cartSubmit');

  function clampQty(value) {
    var number = parseInt(value || '0', 10);
    if (Number.isNaN(number) || number < 0) number = 0;
    return Math.min(number, 9999);
  }

  function getHiddenQty(productId) {
    return orderForm ? orderForm.querySelector('input[type="hidden"][name="qty_' + productId + '"]') : null;
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
  }

  function selectedItems() {
    if (!orderForm) return [];
    return Array.from(orderForm.querySelectorAll('input[type="hidden"][name^="qty_"]')).map(function (input) {
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
    orderForm.addEventListener('click', function (event) {
      var plus = event.target.closest('.qty-plus');
      var minus = event.target.closest('.qty-minus');
      var remove = event.target.closest('.cart-remove');
      if (plus || minus || remove) {
        var productId = (plus || minus || remove).getAttribute('data-product-id');
        var current = getHiddenQty(productId);
        var value = current ? clampQty(current.value) : 0;
        if (plus) setProductQty(productId, value + 1);
        if (minus) setProductQty(productId, value - 1);
        if (remove) setProductQty(productId, 0);
        renderCart();
      }
    });
    orderForm.addEventListener('input', updateOrderCount);
    orderForm.addEventListener('change', function (event) {
      if (event.target.matches('.qty-display[data-product-id]')) {
        setProductQty(event.target.getAttribute('data-product-id'), event.target.value);
        renderCart();
      }
    });
    if (reviewOrder && cartReview) {
      reviewOrder.addEventListener('click', function () {
        renderCart();
        cartReview.hidden = false;
      });
    }
    if (cartCancel && cartReview) {
      cartCancel.addEventListener('click', function () {
        cartReview.hidden = true;
      });
    }
    if (cartSubmit) {
      cartSubmit.addEventListener('click', function () {
        if (!selectedItems().length) {
          renderCart();
          return;
        }
        if (orderForm.requestSubmit) {
          orderForm.requestSubmit();
        } else {
          orderForm.submit();
        }
      });
    }
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
  function updateBulkSelectionState() {
    if (!bulkChecks.length) return;
    var selected = bulkChecks.filter(function (item) { return item.checked; }).length;
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
      bulkChecks.forEach(function (box) { box.checked = selectAllProducts.checked; });
      updateBulkSelectionState();
    });
    bulkChecks.forEach(function (box) {
      box.addEventListener('change', updateBulkSelectionState);
    });
    updateBulkSelectionState();
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

})();
