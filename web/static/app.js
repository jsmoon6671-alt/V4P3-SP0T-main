const state = {
  me: null,
  csrf: "",
  products: [],
  catalogCategories: [],
  cart: [],
  selected: new Set(),
  activeParentCategory: "all",
  activeCategory: "all",
  currentOrder: null,
  timer: null,
  storeResults: [],
  adminCategories: [],
  adminProducts: [],
  pointBalance: 0,
  carriers: [],
  trackingPreset: null,
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[char]);
const productOptions = product => Array.isArray(product.options) ? product.options : [];
const optionSurcharge = option => [...String(option || "").matchAll(/\(\+\s*([\d,]+)\s*(?:원)?\)/g)]
  .reduce((sum, match) => sum + Number(match[1].replaceAll(",", "")), 0);
const itemUnitPrice = item => Number(item.price || 0) + optionSurcharge(item.selected_option);
const CHANNEL_CACHE_KEY = "v4p3DiscordChannelIds";
const SHIPPING_BANNER_KEY = "v4p3ShippingBannerClosed";

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), String(message).length > 55 ? 6000 : 2800);
}

async function api(path, options = {}) {
  const request = {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  };
  if (state.csrf && request.method && request.method !== "GET") {
    request.headers["X-CSRF-Token"] = state.csrf;
  }
  const response = await fetch(path, request);
  const body = await response.text();
  let data = {};
  try { data = body ? JSON.parse(body) : {}; } catch {}
  if (!response.ok) {
    if ((response.status === 404 || response.status === 405) && path.startsWith("/api/admin/")) {
      throw new Error("웹 서버가 이전 버전으로 실행 중입니다. 봇을 완전히 재시작한 뒤 다시 시도해 주세요.");
    }
    throw new Error(data.error || `요청 처리에 실패했습니다. (HTTP ${response.status})`);
  }
  return data;
}

function route() {
  const id = location.hash.slice(1) || "home";
  if (id === "account" && !state.me) {
    location.href = "/auth/login";
    return;
  }
  $$(".page").forEach(page => page.classList.toggle("active", page.id === id));
  $$("#nav a").forEach(link => link.classList.toggle("active", link.getAttribute("href") === `#${id}`));
  if (id === "account" && state.me) loadAccount();
  if (id === "cart" && state.me) {
    loadCart();
    loadCheckoutCustomer();
  }
  if (id === "tracking" && state.me) loadCarriers().then(applyTrackingPreset);
  if (id === "tiers") renderTiers();
  if (id === "admin") openAdminGate();
  scrollTo(0, 0);
}

async function boot() {
  addEventListener("hashchange", route);
  bindForms();
  showDiscordNotice();
  try {
    const data = await api("/api/me");
    state.me = data.user;
    state.csrf = data.csrf;
    $("#login-button").textContent = "내 정보";
    $("#admin-link").hidden = !data.is_admin;
    $("#login-button").onclick = () => { location.hash = "account"; };
    await loadCart();
    if ((location.hash.slice(1) || "home") === "cart") await loadCheckoutCustomer();
  } catch {
    $("#login-button").onclick = () => { location.href = "/auth/login"; };
  }
  try {
    await loadCatalog();
  } catch (error) {
    toast(error.message);
  }
  route();
  loadReviews().catch(error => {
    const track = $("#review-track");
    if (track) track.innerHTML = '<p class="review-empty">구매 후기를 불러오지 못했습니다. 잠시 후 새로고침해 주세요.</p>';
    console.error("구매후기를 불러오지 못했습니다.", error);
  });
}

async function logout() {
  await api("/api/logout", { method: "POST", body: "{}" });
  location.reload();
}

async function loadCatalog() {
  const data = await api("/api/catalog");
  state.products = data.products;
  state.catalogCategories = Array.isArray(data.categories) ? data.categories : [];
  const validCategoryIds = new Set(state.catalogCategories.map(category => String(category.id)));
  if (state.activeParentCategory !== "all" && !validCategoryIds.has(String(state.activeParentCategory))) {
    state.activeParentCategory = "all";
    state.activeCategory = "all";
  }
  if (state.activeCategory !== "all" && !validCategoryIds.has(String(state.activeCategory))) {
    state.activeCategory = "all";
  }
  renderTabs();
  renderProducts();
}

const MEMBERSHIP_TIERS = [
  { name: "VAPE GOD", required: 1000000, tone: "god", icon: "♛" },
  { name: "VAPE MASTER", required: 800000, tone: "master", icon: "✦" },
  { name: "ROYAL", required: 500000, tone: "royal", icon: "◆" },
  { name: "SVIP", required: 300000, tone: "svip", icon: "★" },
  { name: "VVIP", required: 100000, tone: "vvip", icon: "✧" },
  { name: "VIP", required: 50000, tone: "vip", icon: "●" },
];

function renderTiers() {
  const grid = $("#tier-grid");
  if (!grid) return;
  grid.innerHTML = MEMBERSHIP_TIERS.map((tier, index) => `
    <article class="tier-card tier-${tier.tone}">
      <div class="tier-card-top"><span class="tier-icon">${tier.icon}</span><span class="tier-rank">LEVEL ${String(MEMBERSHIP_TIERS.length - index).padStart(2, "0")}</span></div>
      <h2>${tier.name}</h2>
      <p class="tier-condition">누적 구매 금액</p>
      <strong>${money(tier.required)} 이상</strong>
      <div class="tier-benefit"><span>기본 혜택</span><b>등급 할인 없음</b></div>
    </article>
  `).join("");
}

async function loadReviews() {
  const section = $("#review-showcase");
  const track = $("#review-track");
  if (!section || !track) return;
  section.hidden = false;

  const data = await api("/api/reviews");
  const reviews = (Array.isArray(data.reviews) ? data.reviews : []).filter(review => (
    Number(review.rating) === 5
    && String(review.content || "").trim().length >= 5
  ));
  if (!reviews.length) {
    track.innerHTML = '<p class="review-empty">아직 공개할 수 있는 별점 5점 후기가 없습니다.</p>';
    return;
  }

  const minimumCards = 6;
  const repeats = Math.max(1, Math.ceil(minimumCards / reviews.length));
  const loopReviews = Array.from({ length: repeats }, () => reviews).flat();
  const cards = loopReviews.map(review => {
    const imageUrl = String(review.image_url || "").trim();
    return `
    <article class="review-card${imageUrl ? "" : " review-card-text"}">
      ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="구매후기 사진" loading="lazy">` : ""}
      <div class="review-card-body">
        <div class="review-stars" aria-label="별점 5점">★★★★★</div>
        <p>${escapeHtml(String(review.content).trim())}</p>
      </div>
    </article>`;
  }).join("");

  track.style.setProperty("--review-duration", `${Math.max(30, loopReviews.length * 7)}s`);
  track.innerHTML = `<div class="review-group">${cards}</div><div class="review-group" aria-hidden="true">${cards}</div>`;
  section.hidden = false;
}

function renderTabs() {
  const categories = state.catalogCategories;
  const roots = categories.filter(category => !category.parent_id);
  const rootIds = new Set(roots.map(category => Number(category.id)));
  const orphanRoots = categories.filter(category => category.parent_id && !rootIds.has(Number(category.parent_id)));
  const parentTabs = [{ id: "all", name: "전체" }, ...roots, ...orphanRoots];
  $("#parent-category-tabs").innerHTML = parentTabs.map(category => (
    `<button class="${String(category.id) === String(state.activeParentCategory) ? "active" : ""}" data-parent-cat="${category.id}">${escapeHtml(category.name)}</button>`
  )).join("");
  $$("[data-parent-cat]").forEach(button => {
    button.onclick = () => {
      state.activeParentCategory = button.dataset.parentCat;
      state.activeCategory = "all";
      renderTabs();
      renderProducts();
    };
  });

  const activeParent = Number(state.activeParentCategory);
  const children = Number.isFinite(activeParent)
    ? categories.filter(category => Number(category.parent_id) === activeParent)
    : [];
  const childBox = $("#category-tabs");
  childBox.hidden = children.length === 0;
  childBox.innerHTML = children.length ? [{ id: "all", name: "전체" }, ...children].map(category => (
    `<button class="${String(category.id) === String(state.activeCategory) ? "active" : ""}" data-cat="${category.id}">${escapeHtml(category.name)}</button>`
  )).join("") : "";
  $$("[data-cat]", childBox).forEach(button => {
    button.onclick = () => {
      state.activeCategory = button.dataset.cat;
      renderTabs();
      renderProducts();
    };
  });
}

function renderProducts() {
  const rows = state.products.filter(product => {
    if (state.activeParentCategory === "all") return true;
    const inParent = Number(product.category_id) === Number(state.activeParentCategory)
      || Number(product.parent_id) === Number(state.activeParentCategory);
    if (!inParent) return false;
    return state.activeCategory === "all" || Number(product.category_id) === Number(state.activeCategory);
  });
  const activeParent = state.catalogCategories.find(category => Number(category.id) === Number(state.activeParentCategory));
  const activeChild = state.catalogCategories.find(category => Number(category.id) === Number(state.activeCategory));
  $("#catalog-title").textContent = activeChild?.name || activeParent?.name || "전체 상품";
  $("#products").innerHTML = rows.length ? rows.map(product => {
    const options = productOptions(product);
    const optionSelect = options.length ? `
      <label class="product-option">${escapeHtml(product.option_label || "옵션")}
        <select data-product-option="${product.id}">
          <option value="">선택해 주세요</option>
          ${options.map(option => `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`).join("")}
        </select>
      </label>` : "";
    return `<article class="product">
      <div class="product-media">${product.image_url ? `<img src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.name)}">` : "<span>V</span>"}</div>
      <div class="product-body">
        <small>${escapeHtml(product.parent_category_name ? `${product.parent_category_name} · ${product.category_name}` : (product.category_name || "미분류"))}</small>
        <h3>${escapeHtml(product.name)}</h3>
        <p>${escapeHtml(product.description)}</p>
        ${optionSelect}
        <div class="product-foot">
          <div><strong data-product-price="${product.id}">${money(product.price)}</strong></div>
          <button type="button" aria-label="장바구니에 담기" data-add="${product.id}">+</button>
        </div>
      </div>
    </article>`;
  }).join("") : '<p class="empty">등록된 상품이 없습니다.</p>';
  $$("[data-add]").forEach(button => {
    button.onclick = () => addCart(Number(button.dataset.add));
  });
  $$("[data-product-option]").forEach(select => {
    select.onchange = () => {
      const product = state.products.find(item => Number(item.id) === Number(select.dataset.productOption));
      const price = $(`[data-product-price="${select.dataset.productOption}"]`);
      if (product && price) price.textContent = money(Number(product.price) + optionSurcharge(select.value));
    };
  });
}

async function addCart(productId) {
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  const product = state.products.find(item => Number(item.id) === productId);
  const options = productOptions(product || {});
  const selectedOption = $(`[data-product-option="${productId}"]`)?.value || "";
  if (options.length && !selectedOption) {
    toast(`${product.option_label || "옵션"}을 선택해 주세요.`);
    return;
  }
  try {
    await api("/api/cart", {
      method: "POST",
      body: JSON.stringify({ product_id: productId, quantity: 1, selected_option: selectedOption }),
    });
    toast("장바구니에 담았습니다.");
    await loadCart();
  } catch (error) {
    toast(error.message);
  }
}

async function loadCart() {
  if (!state.me) return;
  const data = await api("/api/cart");
  state.cart = data.items;
  state.selected = new Set(data.items.map(item => Number(item.product_id)));
  $("#cart-count").textContent = data.items.length;
  renderCart();
}

function renderCart() {
  const box = $("#cart-items");
  if (!box) return;
  box.innerHTML = state.cart.length ? state.cart.map(item => {
    const options = productOptions(item);
    const optionField = options.length ? `<select data-cart-option="${item.product_id}">
      ${options.map(option => `<option value="${escapeHtml(option)}" ${option === item.selected_option ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
    </select>` : "";
    return `<div class="cart-row">
      <input class="cart-check" type="checkbox" aria-label="상품 선택" data-select="${item.product_id}" ${state.selected.has(Number(item.product_id)) ? "checked" : ""}>
      <div class="grow"><strong>${escapeHtml(item.name)}</strong><br><small>${money(itemUnitPrice(item))}${optionSurcharge(item.selected_option) ? ` · 옵션 +${money(optionSurcharge(item.selected_option))}` : ""}</small>${optionField}</div>
      <div class="cart-controls">
        <div class="quantity-stepper" aria-label="수량 조절">
          <button type="button" aria-label="수량 줄이기" data-qty-minus="${item.product_id}">−</button>
          <input type="number" aria-label="수량" min="1" max="99" value="${item.quantity}" data-qty="${item.product_id}">
          <button type="button" aria-label="수량 늘리기" data-qty-plus="${item.product_id}">+</button>
        </div>
        <button type="button" class="remove" data-remove="${item.product_id}">삭제</button>
      </div>
    </div>`;
  }).join("") : '<div class="empty">장바구니가 비어 있습니다.</div>';

  $$("[data-select]", box).forEach(input => {
    input.onchange = () => {
      input.checked ? state.selected.add(Number(input.dataset.select)) : state.selected.delete(Number(input.dataset.select));
      cartTotal();
    };
  });
  $$("[data-qty]", box).forEach(input => {
    input.onchange = async () => saveCartRow(Number(input.dataset.qty), Number(input.value));
  });
  $$("[data-qty-minus]", box).forEach(button => {
    button.onclick = async () => {
      const input = $(`[data-qty="${button.dataset.qtyMinus}"]`, box);
      const next = Math.max(Number(input.min), Number(input.value) - 1);
      if (next !== Number(input.value)) await saveCartRow(Number(button.dataset.qtyMinus), next);
    };
  });
  $$("[data-qty-plus]", box).forEach(button => {
    button.onclick = async () => {
      const input = $(`[data-qty="${button.dataset.qtyPlus}"]`, box);
      const next = Math.min(Number(input.max), Number(input.value) + 1);
      if (next !== Number(input.value)) await saveCartRow(Number(button.dataset.qtyPlus), next);
    };
  });
  $$("[data-cart-option]", box).forEach(select => {
    select.onchange = async () => {
      const item = state.cart.find(row => Number(row.product_id) === Number(select.dataset.cartOption));
      await saveCartRow(Number(select.dataset.cartOption), Number(item.quantity), select.value);
    };
  });
  $$("[data-remove]", box).forEach(button => {
    button.onclick = async () => {
      await api(`/api/cart/${button.dataset.remove}`, { method: "DELETE", body: "{}" });
      await loadCart();
    };
  });
  cartTotal();
}

async function saveCartRow(productId, quantity, selectedOption = null) {
  const item = state.cart.find(row => Number(row.product_id) === productId);
  try {
    await api("/api/cart", {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        quantity,
        selected_option: selectedOption ?? item.selected_option ?? "",
      }),
    });
    await loadCart();
  } catch (error) {
    toast(error.message);
  }
}

function cartTotal() {
  const selectedRows = state.cart.filter(item => state.selected.has(Number(item.product_id)));
  const subtotal = selectedRows.reduce((sum, item) => sum + itemUnitPrice(item) * Number(item.quantity), 0);
  const shipping = selectedRows.length ? shippingFee(subtotal) : 0;
  const beforeDiscount = subtotal + shipping;
  const discount = checkoutPointDiscount(beforeDiscount);
  const total = Math.max(0, beforeDiscount - discount);
  $("#cart-subtotal").textContent = money(subtotal);
  $("#cart-shipping").textContent = money(shipping);
  $("#cart-discount").textContent = `-${money(discount)}`;
  $("#cart-discount-row").hidden = discount === 0;
  $("#cart-total").textContent = money(total);
  $("#selected-count").textContent = `${selectedRows.length}개 선택`;
  updateShippingBenefit(subtotal, selectedRows.length > 0);
  updatePointUseHint(beforeDiscount);
}

function shippingFee(subtotal) {
  const amount = Math.max(0, Number(subtotal) || 0);
  if (amount >= 50000) return 0;
  if (amount >= 35000) return 1500;
  return amount > 0 ? 3000 : 0;
}

function updateShippingBenefit(subtotal, hasItems) {
  const message = $("#shipping-benefit");
  if (!message) return;
  if (!hasItems) {
    message.textContent = "선택한 상품 금액에 따라 자동 적용됩니다.";
  } else if (subtotal >= 50000) {
    message.textContent = "무료배송이 적용되었습니다.";
  } else if (subtotal >= 35000) {
    message.textContent = `배송비 1,500원이 적용되었습니다. ${money(50000 - subtotal)} 더 담으면 무료배송입니다.`;
  } else {
    message.textContent = `${money(35000 - subtotal)} 더 담으면 배송비가 1,500원으로 할인됩니다.`;
  }
}

async function loadCheckoutCustomer() {
  if (!state.me) return;
  try {
    const data = await api("/api/customer");
    for (const key of ["name", "contact", "address", "cvs"]) {
      const input = $(`#checkout-form [name="${key}"]`);
      if (input && !input.value) input.value = data.customer[key] || "";
    }
    state.pointBalance = Number(data.points || 0);
    $("#checkout-point-balance").textContent = `보유 ${state.pointBalance.toLocaleString()}P`;
    updatePointUseHint();
    cartTotal();
  } catch (error) {
    toast(error.message);
  }
}

function selectedCartRows() {
  return state.cart.filter(item => state.selected.has(Number(item.product_id)));
}

function selectedCartTotal() {
  const rows = selectedCartRows();
  const subtotal = rows.reduce((sum, item) => sum + itemUnitPrice(item) * Number(item.quantity), 0);
  return rows.length ? subtotal + shippingFee(subtotal) : 0;
}

function maximumCheckoutPoints(total = selectedCartTotal()) {
  const balance = Number.isFinite(state.pointBalance) ? Math.max(0, state.pointBalance) : 0;
  const maximum = Math.min(balance, 2000, Math.max(0, Number(total) || 0));
  return maximum >= 500 ? maximum : 0;
}

function checkoutPointDiscount(total = selectedCartTotal()) {
  const input = $("#checkout-form [name=\"points\"]");
  const points = Number(input?.value || 0);
  const maximum = maximumCheckoutPoints(total);
  return Number.isInteger(points) && points >= 500 && points <= maximum ? points : 0;
}

function updatePointUseHint(total = selectedCartTotal()) {
  const hint = $("#point-use-hint");
  if (!hint) return;
  if (!selectedCartRows().length) {
    hint.textContent = "구매할 상품을 먼저 선택해 주세요.";
    return;
  }
  if (state.pointBalance < 500) {
    hint.textContent = "보유 포인트가 500P 미만이라 이번 결제에는 적용할 수 없습니다.";
    return;
  }
  const maximum = maximumCheckoutPoints(total);
  hint.textContent = maximum
    ? `500P부터 최대 ${maximum.toLocaleString()}P까지 사용할 수 있습니다. (1P = 1원)`
    : "결제금액이 500원 미만이라 포인트를 적용할 수 없습니다.";
}

function useAllPoints() {
  if (!selectedCartRows().length) {
    toast("먼저 주문 상품에서 구매할 상품을 선택해 주세요.");
    return;
  }
  if (state.pointBalance < 500) {
    toast("보유 포인트가 500P 미만이라 사용할 수 없습니다.");
    return;
  }
  const maximum = maximumCheckoutPoints();
  if (maximum < 500) {
    toast("결제금액이 500원 미만이라 포인트를 적용할 수 없습니다.");
    return;
  }
  $("#checkout-form [name=\"points\"]").value = maximum;
  cartTotal();
  toast(`${maximum.toLocaleString()}P를 적용했습니다.`);
}

async function loadCarriers() {
  const select = $("#tracking-carrier");
  if (!select || state.carriers.length) return;
  try {
    const data = await api("/api/tracking/carriers");
    state.carriers = data.carriers || [];
    select.innerHTML = '<option value="">택배사를 선택해 주세요</option>' + state.carriers.map(carrier => (
      `<option value="${escapeHtml(carrier.id)}">${escapeHtml(carrier.name)}</option>`
    )).join("");
  } catch (error) {
    select.innerHTML = '<option value="">택배사를 불러오지 못했습니다</option>';
    toast(error.message);
  }
}

function applyTrackingPreset() {
  if (!state.trackingPreset) return;
  const preset = state.trackingPreset;
  state.trackingPreset = null;
  const form = $("#tracking-form");
  form.elements.waybill.value = preset.waybill || "";
  form.elements.carrier_id.value = preset.carrier || "";
  if (preset.carrier && form.elements.carrier_id.value) form.requestSubmit();
  else form.elements.carrier_id.focus();
}

async function trackDelivery(event) {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target));
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "조회 중";
  try {
    const data = await api("/api/tracking", { method: "POST", body: JSON.stringify(form) });
    renderTrackingResult(data);
  } catch (error) {
    $("#tracking-result").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "배송 조회하기";
  }
}

function renderTrackingResult(data, target = "#tracking-result") {
  const shipment = data.shipment || {};
  const history = Array.isArray(shipment.history) ? shipment.history : [];
  const box = typeof target === "string" ? $(target) : target;
  box.innerHTML = `
    <div class="tracking-head"><div><small>${escapeHtml(data.carrier_name || "택배사")}</small><h2>${escapeHtml(shipment.status || "배송 상태 확인 중")}</h2></div><span>${escapeHtml(data.waybill || "")}</span></div>
    <dl class="tracking-meta"><div><dt>현재 위치</dt><dd>${escapeHtml(shipment.location || "정보 미제공")}</dd></div><div><dt>받는 분</dt><dd>${escapeHtml(shipment.receiver || "정보 미제공")}</dd></div></dl>
    ${data.delivered ? '<div class="review-reminder"><strong>상품은 잘 받아보셨나요?</strong><p>배송이 완료되었습니다. Discord에서 <code>/후기작성</code> 명령어로 후기와 사진을 남겨 주세요.</p></div>' : ""}
    <div class="tracking-history">${history.length ? history.map(item => `<article><i></i><div><strong>${escapeHtml(item.description || item.status || "배송 처리")}</strong><span>${escapeHtml(item.location || "위치 정보 없음")} · ${escapeHtml(item.time || "시간 정보 없음")}</span></div></article>`).join("") : '<div class="empty">아직 등록된 배송 이력이 없습니다.</div>'}</div>`;
}

function updateShippingMode() {
  const method = $("#shipping-method").value;
  const convenience = method === "GS25" || method === "CU";
  $("#store-lookup").hidden = !convenience;
  $("#shipping-address").readOnly = convenience;
  if (!convenience) {
    $("#shipping-cvs").value = "";
    state.storeResults = [];
  } else {
    $("#shipping-address").value = "";
    $("#shipping-cvs").value = "";
    $("#store-results").innerHTML = '<option value="">편의점명을 조회한 뒤 선택해 주세요</option>';
  }
}

async function searchStores() {
  const brand = $("#shipping-method").value;
  const query = $("#store-query").value.trim();
  if (!query) {
    toast("편의점 지점명을 입력해 주세요.");
    return;
  }
  const button = $("#store-search");
  button.disabled = true;
  button.textContent = "조회 중";
  try {
    const data = await api(`/api/stores?brand=${encodeURIComponent(brand)}&q=${encodeURIComponent(query)}`);
    state.storeResults = data.stores;
    $("#store-results").innerHTML = '<option value="">받을 편의점을 선택해 주세요</option>' + data.stores.map((store, index) => (
      `<option value="${index}">${escapeHtml(store.name)} · ${escapeHtml(store.address)}</option>`
    )).join("");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "주소 조회";
  }
}

function chooseStore() {
  const index = Number($("#store-results").value);
  const store = state.storeResults[index];
  if (!store) return;
  $("#shipping-cvs").value = store.name;
  $("#shipping-address").value = store.address;
}

async function loadAccount() {
  if (!state.me) return;
  try {
    const [customer, orders] = await Promise.all([api("/api/customer"), api("/api/orders")]);
    Object.entries(customer.customer).forEach(([key, value]) => {
      const input = $(`#customer-form [name="${key}"]`);
      if (input) input.value = value || "";
    });
    state.pointBalance = Number(customer.points || 0);
    $("#point-balance").textContent = `${state.pointBalance.toLocaleString()}P`;
    $("#checkout-point-balance").textContent = `보유 ${state.pointBalance.toLocaleString()}P`;
    cartTotal();
    $("#orders").innerHTML = orders.orders.length ? orders.orders.map(order => {
      const progress = orderProgress(order);
      return `<article class="order">
      <div class="order-head"><strong>${escapeHtml(order.order_id)}</strong><span class="badge ${order.status}">${statusText(order.status)}</span></div>
      <p>${escapeHtml(order.product)}</p>
      <small>${escapeHtml(order.amount)} · ${String(order.created_at).slice(0, 16).replace("T", " ")}</small>
      <div class="order-progress"><div><span>${escapeHtml(progress.label)}</span><b>${progress.percent}%</b></div><div class="progress-track"><i style="width:${progress.percent}%"></i></div></div>
      ${order.waybill_number ? `<div class="order-actions"><span>운송장 ${escapeHtml(order.waybill_number)}</span><button type="button" class="text-button" data-order-detail="${escapeHtml(order.order_id)}">상세조회</button></div><div class="order-detail" data-order-detail-box="${escapeHtml(order.order_id)}" hidden></div>` : ""}
    </article>`;
    }).join("") : '<div class="empty">아직 주문내역이 없습니다.</div>';
    $$("[data-order-detail]").forEach(button => {
      button.onclick = () => toggleOrderTracking(button);
    });
  } catch (error) {
    if (error.message.includes("로그인")) location.href = "/auth/login";
    else toast(error.message);
  }
}

function orderProgress(order) {
  if (order.status === "PENDING") return { label: "입금 대기", percent: 0 };
  if (["CANCELLED", "REJECTED"].includes(order.status)) return { label: statusText(order.status), percent: 0 };
  return ({
    PAYMENT_APPROVED: { label: "승인완료", percent: 20 },
    PRODUCT_PREPARING: { label: "상품준비중", percent: 40 },
    SHIPPING_PREPARING: { label: "배송준비중", percent: 60 },
    SHIPPING: { label: "배송중", percent: 80 },
    DELIVERED: { label: "배송완료", percent: 100 },
  })[order.fulfillment_status || "PAYMENT_APPROVED"] || { label: "처리중", percent: 20 };
}

async function toggleOrderTracking(button) {
  const orderId = button.dataset.orderDetail;
  const box = $(`[data-order-detail-box="${orderId}"]`);
  if (!box.hidden) {
    box.hidden = true;
    button.textContent = "상세조회";
    return;
  }
  box.hidden = false;
  button.textContent = "상세 닫기";
  box.innerHTML = '<div class="empty">배송 정보를 불러오는 중입니다.</div>';
  try {
    const data = await api(`/api/orders/${encodeURIComponent(orderId)}/tracking`);
    renderTrackingResult(data, box);
  } catch (error) {
    box.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
  setTimeout(() => box.scrollIntoView({ behavior: "smooth", block: "nearest" }), 50);
}

const statusText = status => ({
  PENDING: "입금 대기", APPROVED: "구매 완료", CANCELLED: "자동 취소", REJECTED: "거절",
  SHIPPING_READY: "배송 준비중", SHIPPING: "배송중", DELIVERED: "배송완료",
})[status] || status;

function bindForms() {
  bindShippingBanner();
  restoreChannelForm();
  $$("#channel-form input").forEach(input => {
    input.addEventListener("input", () => cacheChannelForm());
  });
  $("#customer-form").onsubmit = async event => {
    event.preventDefault();
    try {
      await api("/api/customer", { method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(event.target))) });
      toast("고객정보를 저장했습니다.");
    } catch (error) {
      toast(error.message);
    }
  };
  $("#account-logout").onclick = logout;
  $("#checkout-form").onsubmit = checkout;
  $("#use-all-points").onclick = useAllPoints;
  $("#checkout-form [name=\"points\"]").oninput = cartTotal;
  $("#tracking-form").onsubmit = trackDelivery;
  $("#channel-form").onsubmit = saveChannels;
  $("#shipping-method").onchange = updateShippingMode;
  $("#store-search").onclick = searchStores;
  $("#store-results").onchange = chooseStore;
  $("#add-parent-category").onclick = () => openEditor("category", { parentMode: true });
  $("#add-category").onclick = () => openEditor("category", { childMode: true });
  $("#add-manual-product").onclick = () => openEditor("product", { manualCreate: true });
  $("#add-product").onclick = () => openEditor("product");
  $("#sync-products").onclick = syncProducts;
  $("#editor-close").onclick = closeEditor;
  $("#editor").onclick = event => { if (event.target === $("#editor")) closeEditor(); };
  $("#admin-password-form").onsubmit = unlockAdmin;
  $("#admin-gate-close").onclick = () => { closeAdminGate(); location.hash = "home"; };
  $("#notice-close").onclick = closeDiscordNotice;
  $("#notice-day").onclick = dismissDiscordNoticeForDay;
  addEventListener("keydown", event => {
    if (event.key === "Escape" && !$("#editor").hidden) closeEditor();
  });
  updateShippingMode();
}

function bindShippingBanner() {
  const banner = $("#shipping-banner");
  const close = $("#shipping-banner-close");
  if (!banner || !close) return;
  try {
    banner.hidden = sessionStorage.getItem(SHIPPING_BANNER_KEY) === "1";
  } catch {}
  close.onclick = () => {
    banner.hidden = true;
    try { sessionStorage.setItem(SHIPPING_BANNER_KEY, "1"); } catch {}
  };
}

async function checkout(event) {
  event.preventDefault();
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  const form = Object.fromEntries(new FormData(event.target));
  form.order_confirmed = Boolean(form.order_confirmed);
  form.product_ids = [...state.selected];
  const points = Number(form.points || 0);
  const maximum = maximumCheckoutPoints();
  if (!Number.isInteger(points) || points < 0 || points > 2000 || (points > 0 && points < 500)) {
    toast("포인트는 사용하지 않으려면 0P, 사용할 때는 500P~2,000P를 입력해 주세요.");
    return;
  }
  if (points > maximum) {
    toast(`현재 이 주문에 사용할 수 있는 포인트는 최대 ${maximum.toLocaleString()}P입니다.`);
    return;
  }
  try {
    const data = await api("/api/checkout", { method: "POST", body: JSON.stringify(form) });
    state.currentOrder = data.order_id;
    $("#complete-eyebrow").textContent = "PAYMENT WAITING";
    $("#complete-title").textContent = "입금을 기다리고 있습니다.";
    $("#countdown").hidden = false;
    $("#bank-info").hidden = false;
    $("#complete-order").textContent = data.order_id;
    $("#bank-info").innerHTML = data.cash_amount
      ? `<strong>${escapeHtml(data.bank.bank_name || "은행 미설정")} ${escapeHtml(data.bank.account_number || "")}</strong><br>${escapeHtml(data.bank.account_holder || "")} · ${money(data.cash_amount)}`
      : "포인트 전액결제로 승인되었습니다.";
    location.hash = "complete";
    startCountdown(data);
  } catch (error) {
    toast(error.message);
  }
}

function startCountdown(data) {
  clearInterval(state.timer);
  let left = data.status === "APPROVED" ? 0 : data.deadline_seconds;
  const draw = () => {
    $("#countdown").textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
  };
  draw();
  if (data.status === "APPROVED") {
    showPaymentApproved("결제가 승인되었습니다.");
    return;
  }
  state.timer = setInterval(async () => {
    left = Math.max(0, left - 1);
    draw();
    if (left % 2 === 0 || left === 0) {
      try {
        const order = await api(`/api/orders/${state.currentOrder}`);
        if (order.status === "APPROVED") {
          showPaymentApproved("입금이 확인되어 자동 승인되었습니다.");
          clearInterval(state.timer);
        } else if (order.status === "CANCELLED") {
          $("#payment-status").textContent = "5분 안에 입금이 확인되지 않아 주문이 취소되었습니다.";
          clearInterval(state.timer);
        }
      } catch {}
    }
  }, 1000);
}

function showPaymentApproved(message) {
  $("#complete-eyebrow").textContent = "PAYMENT APPROVED";
  $("#complete-title").textContent = "승인완료";
  $("#countdown").hidden = true;
  $("#bank-info").hidden = true;
  $("#payment-status").textContent = message;
}

function openAdminGate() {
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  $("#admin-gate").hidden = false;
  document.body.classList.add("modal-open");
  setTimeout(() => $('#admin-password-form input[name="password"]').focus(), 0);
}

function closeAdminGate() {
  $("#admin-gate").hidden = true;
  document.body.classList.remove("modal-open");
  $("#admin-password-form").reset();
}

async function unlockAdmin(event) {
  event.preventDefault();
  const password = new FormData(event.target).get("password");
  try {
    await api("/api/admin/unlock", { method: "POST", body: JSON.stringify({ password }) });
    closeAdminGate();
    await loadAdmin();
  } catch (error) {
    toast(error.message);
    event.target.querySelector('input[name="password"]').select();
  }
}

async function loadAdmin(period = "week") {
  if (!state.me) return;
  const results = await Promise.allSettled([
    api(`/api/admin/dashboard?period=${period}`),
    api("/api/admin/categories"),
    api("/api/admin/products"),
    api("/api/admin/channels"),
  ]);
  const failures = results.filter(result => result.status === "rejected");
  const passwordFailure = failures.find(result => result.reason?.message?.includes("비밀번호"));
  if (passwordFailure) {
    openAdminGate();
    return;
  }

  const [dashboardResult, categoriesResult, productsResult, channelsResult] = results;
  if (categoriesResult.status === "fulfilled") {
    state.adminCategories = categoriesResult.value.categories;
  }
  if (productsResult.status === "fulfilled") {
    state.adminProducts = productsResult.value.products;
  }
  renderAdminLists();

  if (dashboardResult.status === "fulfilled") {
    const dashboard = dashboardResult.value;
    $("#metrics").innerHTML = [
      ["총 매출", money(dashboard.summary.revenue)], ["승인", dashboard.summary.approved],
      ["대기", dashboard.summary.pending], ["취소", dashboard.summary.cancelled],
    ].map(item => `<div class="metric"><small>${item[0]}</small><strong>${item[1]}</strong></div>`).join("");
    $("#trend-title").textContent = { week: "최근 7일 매출", month: "최근 30일 매출", year: "최근 12개월 매출" }[dashboard.period];
    $$("[data-period]").forEach(button => {
      button.classList.toggle("active", button.dataset.period === dashboard.period);
      button.onclick = () => loadAdmin(button.dataset.period);
    });
    const max = Math.max(1, ...dashboard.trend.map(item => Number(item.value)));
    $("#trend").innerHTML = dashboard.trend.map(item => `<div class="bar" style="height:${Math.max(3, Number(item.value) / max * 100)}%"><span>${item.label}</span></div>`).join("");
    const groups = Object.groupBy ? Object.groupBy(dashboard.top, item => item.category) : dashboard.top.reduce((result, item) => ((result[item.category] ??= []).push(item), result), {});
    $("#top-products").innerHTML = Object.entries(groups).map(([category, items]) => `<h3>${escapeHtml(category)}</h3>${items.map(item => `<div class="admin-row"><span>${escapeHtml(item.product_name)}</span><b>${item.quantity}개</b></div>`).join("")}`).join("") || '<p class="empty">판매 데이터가 없습니다.</p>';
  }

  if (channelsResult.status === "fulfilled") {
    fillChannelForm(channelsResult.value.channels);
    cacheChannelForm(channelsResult.value.channels);
  }
  if (failures.length) toast(failures[0].reason?.message || "관리자 정보를 일부 불러오지 못했습니다.");
}

function fillChannelForm(channels) {
  Object.entries(channels || {}).forEach(([key, value]) => {
    const input = $(`#channel-form [name="${key}"]`);
    if (input) input.value = value || "";
  });
}

function channelFormValues() {
  return Object.fromEntries(new FormData($("#channel-form")));
}

function cacheChannelForm(channels = channelFormValues()) {
  try {
    localStorage.setItem(CHANNEL_CACHE_KEY, JSON.stringify(channels));
  } catch {}
}

function restoreChannelForm() {
  try {
    const cached = JSON.parse(localStorage.getItem(CHANNEL_CACHE_KEY) || "{}");
    fillChannelForm(cached);
  } catch {}
}

function renderAdminLists() {
  const categoryRows = [];
  const roots = state.adminCategories.filter(category => !category.parent_id);
  const rootIds = new Set(roots.map(category => Number(category.id)));
  [...roots, ...state.adminCategories.filter(category => category.parent_id && !rootIds.has(Number(category.parent_id)))].forEach(parent => {
    categoryRows.push(`<div class="admin-row category-parent-row"><span><b>${escapeHtml(parent.name)}</b><small>대분류</small></span><span><button class="text-button" data-edit-cat="${parent.id}">수정</button> <button class="remove" data-del-cat="${parent.id}">삭제</button></span></div>`);
    state.adminCategories.filter(category => Number(category.parent_id) === Number(parent.id)).forEach(category => {
      categoryRows.push(`<div class="admin-row category-child-row"><span><i>↳</i> ${escapeHtml(category.name)}</span><span><button class="text-button" data-edit-cat="${category.id}">수정</button> <button class="remove" data-del-cat="${category.id}">삭제</button></span></div>`);
    });
  });
  $("#category-admin").innerHTML = categoryRows.join("") || '<p class="admin-empty">등록된 카테고리가 없습니다.</p>';
  const productRows = state.adminProducts.map(product => {
    const options = productOptions(product);
    const categoryPath = product.parent_category_name ? `${product.parent_category_name} › ${product.category_name}` : (product.category_name || "미분류");
    return `<div class="admin-row"><span class="admin-product-info"><input class="admin-product-check" type="checkbox" value="${product.id}" aria-label="${escapeHtml(product.name)} 선택"><span><b>${escapeHtml(product.name)}</b><br>${escapeHtml(categoryPath)} · ${money(product.price)}${options.length ? `<br><small>${escapeHtml(product.option_label)}: ${options.map(escapeHtml).join(", ")}</small>` : ""}</span></span><span class="admin-actions"><button class="text-button" data-edit-product="${product.id}">수정</button><button class="remove" data-del-product="${product.id}">삭제</button></span></div>`;
  }).join("");
  $("#product-admin").innerHTML = `<div class="admin-bulk-actions">
    <label><input id="select-all-products" type="checkbox" ${state.adminProducts.length ? "" : "disabled"}> 전체 선택</label>
    <button id="delete-selected-products" class="button danger compact" type="button" disabled>선택 삭제</button>
  </div>${productRows || '<p class="admin-empty">등록된 상품이 없습니다.</p>'}`;

  const productChecks = $$(".admin-product-check");
  const selectAll = $("#select-all-products");
  const deleteSelected = $("#delete-selected-products");
  const updateBulkSelection = () => {
    const selectedCount = productChecks.filter(input => input.checked).length;
    selectAll.checked = productChecks.length > 0 && selectedCount === productChecks.length;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < productChecks.length;
    deleteSelected.disabled = selectedCount === 0;
    deleteSelected.textContent = selectedCount ? `선택 삭제 (${selectedCount})` : "선택 삭제";
  };
  selectAll.onchange = () => {
    productChecks.forEach(input => { input.checked = selectAll.checked; });
    updateBulkSelection();
  };
  productChecks.forEach(input => { input.onchange = updateBulkSelection; });
  deleteSelected.onclick = async () => {
    const productIds = productChecks.filter(input => input.checked).map(input => Number(input.value));
    if (!productIds.length || !confirm(`선택한 상품 ${productIds.length}개를 모두 삭제할까요?\n상품 목록과 장바구니에서 제거되며 기존 주문내역은 유지됩니다.`)) return;
    deleteSelected.disabled = true;
    try {
      const result = await api("/api/admin/products/delete-batch", {
        method: "POST",
        body: JSON.stringify({ product_ids: productIds }),
      });
      await Promise.all([loadAdmin(), loadCatalog(), loadCart()]);
      toast(`상품 ${result.deleted}개를 삭제했습니다.`);
    } catch (error) {
      deleteSelected.disabled = false;
      toast(error.message);
    }
  };
  $$("[data-edit-cat]").forEach(button => { button.onclick = () => openEditor("category", state.adminCategories.find(item => item.id == button.dataset.editCat)); });
  $$("[data-del-cat]").forEach(button => {
    button.onclick = async () => {
      if (!confirm("카테고리를 삭제할까요?")) return;
      try {
        await api(`/api/admin/categories/${button.dataset.delCat}`, { method: "DELETE", body: "{}" });
        await loadAdmin();
        await loadCatalog();
        toast("카테고리를 삭제했습니다.");
      } catch (error) {
        toast(error.message);
      }
    };
  });
  $$("[data-edit-product]").forEach(button => { button.onclick = () => openEditor("product", state.adminProducts.find(item => item.id == button.dataset.editProduct)); });
  $$("[data-del-product]").forEach(button => {
    button.onclick = async () => {
      const product = state.adminProducts.find(item => item.id == button.dataset.delProduct);
      if (!product || !confirm(`"${product.name}" 상품을 삭제할까요?\n상품 목록과 장바구니에서 제거되며 기존 주문내역은 유지됩니다.`)) return;
      try {
        await api(`/api/admin/products/${button.dataset.delProduct}`, { method: "DELETE", body: "{}" });
        await Promise.all([loadAdmin(), loadCatalog(), loadCart()]);
        toast(`"${product.name}" 상품을 삭제했습니다.`);
      } catch (error) {
        toast(error.message);
      }
    };
  });
}

function closeEditor() {
  $("#editor").hidden = true;
  document.body.classList.remove("modal-open");
}

function productCategoryOptions(selectedId = null) {
  const parentIds = new Set(state.adminCategories.filter(category => category.parent_id).map(category => Number(category.parent_id)));
  return state.adminCategories
    .filter(category => !parentIds.has(Number(category.id)))
    .map(category => {
      const label = category.parent_name ? `${category.parent_name} › ${category.name}` : category.name;
      return `<option value="${category.id}" ${Number(category.id) === Number(selectedId) ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
}

function openEditor(type, item = {}) {
  const fields = $("#editor-fields");
  const isManualCreate = type === "product" && Boolean(item.manualCreate);
  const isLinkImport = type === "product" && !item.id && !isManualCreate;
  $("#editor-title").textContent = type === "category"
    ? item.parentMode ? "대분류 추가" : item.childMode ? "하위 카테고리 추가" : "카테고리 설정"
    : isLinkImport ? "링크로 상품 추가" : isManualCreate ? "직접 상품 추가" : "상품 설정";
  if (type === "category") {
    const rootCategories = state.adminCategories.filter(category => !category.parent_id && Number(category.id) !== Number(item.id));
    const parentField = item.parentMode
      ? '<input type="hidden" name="parent_id" value="">'
      : `<label>상위 대분류<select name="parent_id" ${item.childMode ? "required" : ""}>
          <option value="">${item.childMode ? "대분류를 선택해 주세요" : "대분류로 사용"}</option>
          ${rootCategories.map(category => `<option value="${category.id}" ${Number(category.id) === Number(item.parent_id) ? "selected" : ""}>${escapeHtml(category.name)}</option>`).join("")}
        </select></label>`;
    fields.innerHTML = `<input type="hidden" name="id" value="${item.id || ""}">
      <label>이름<input name="name" value="${escapeHtml(item.name || "")}" required></label>
      ${parentField}
      <label>정렬 순서<input name="sort_order" type="number" value="${item.sort_order || 0}"></label>`;
  } else if (isLinkImport) {
    fields.innerHTML = `<label>추가할 카테고리
        <select name="category_id" required>
          <option value="">카테고리를 먼저 선택해 주세요</option>
          ${productCategoryOptions()}
        </select>
      </label>
      <label>상품 또는 목록 링크
        <input name="source_url" type="url" inputmode="url" placeholder="https://..." autocomplete="off" required>
      </label>
      <label>판매 가격
        <input name="price" type="number" inputmode="numeric" min="0" step="1" placeholder="직접 판매할 가격을 입력해 주세요" required>
      </label>
      <label>옵션 종류
        <select name="option_label"><option value="없음">없음</option><option value="옵션">옵션</option><option value="색상">색상</option><option value="맛">맛</option><option value="패키지">패키지</option></select>
      </label>
      <label data-option-list>옵션 목록
        <textarea name="options" placeholder="예: 기본 패키지&#10;선물 패키지 (+2000)&#10;여러 값은 한 줄씩 또는 쉼표로 입력"></textarea>
      </label>
      <small data-option-help>옵션명에 (+2000)을 붙이면 선택 시 개당 2,000원이 추가됩니다.</small>
      <small>비비빈스와 일렉샵 링크를 지원합니다. 입력한 가격은 자동 동기화 후에도 유지됩니다. 목록 링크를 사용하면 가져온 모든 상품에 같은 가격과 옵션이 적용됩니다.</small>`;
  } else {
    fields.innerHTML = `<input type="hidden" name="id" value="${item.id || ""}">
      <label>카테고리
        <select name="category_id" required>
          <option value="">카테고리를 먼저 선택해 주세요</option>
          ${productCategoryOptions(item.category_id)}
        </select>
      </label>
      <label>상품명<input name="name" value="${escapeHtml(item.name || "")}" required></label>
      <label>상품 이미지 URL<input id="product-image-url" name="image_url" value="${escapeHtml(item.image_url || "")}" placeholder="이미지 주소 또는 아래 파일 선택" required></label>
      <label>이미지 파일<input id="product-image-file" type="file" accept="image/*"></label>
      <label>설명<textarea name="description">${escapeHtml(item.description || "")}</textarea></label>
      <label>가격<input name="price" type="number" min="0" value="${item.price || 0}" required></label>
      <label>옵션 종류
        <select name="option_label" required><option value="없음" ${item.option_label === "없음" ? "selected" : ""}>없음</option><option value="옵션" ${item.option_label === "옵션" ? "selected" : ""}>옵션</option><option value="색상" ${item.option_label === "색상" || !item.option_label ? "selected" : ""}>색상</option><option value="맛" ${item.option_label === "맛" ? "selected" : ""}>맛</option><option value="패키지" ${item.option_label === "패키지" ? "selected" : ""}>패키지</option></select>
      </label>
      <label data-option-list>옵션 목록<textarea name="options" placeholder="예: 기본 옵션&#10;추가 옵션 (+2000)" required>${escapeHtml(productOptions(item).join("\n"))}</textarea></label>
      <small data-option-help>옵션명에 (+2000)을 붙이면 선택 시 개당 2,000원이 추가됩니다.</small>
      <label class="check"><input name="is_active" type="checkbox" ${item.is_active !== false ? "checked" : ""}> 판매 활성화</label>`;
  }
  $("#editor-form button[type='submit']").textContent = isLinkImport ? "링크에서 가져오기" : "저장";
  $("#editor").hidden = false;
  document.body.classList.add("modal-open");
  if (type === "product") {
    const optionType = fields.querySelector('[name="option_label"]');
    const optionList = fields.querySelector("[data-option-list]");
    const optionInput = optionList?.querySelector('[name="options"]');
    const optionHelp = fields.querySelector("[data-option-help]");
    const syncOptionFields = () => {
      const hasNoOptions = optionType?.value === "없음";
      if (optionList) optionList.hidden = hasNoOptions;
      if (optionHelp) optionHelp.hidden = hasNoOptions;
      if (optionInput) {
        optionInput.disabled = hasNoOptions;
        optionInput.required = !isLinkImport && !hasNoOptions;
      }
    };
    optionType?.addEventListener("change", syncOptionFields);
    syncOptionFields();
  }
  if (type === "product" && !isLinkImport) {
    $("#product-image-file").onchange = event => {
      const file = event.target.files[0];
      if (!file) return;
      if (file.size > 5 * 1024 * 1024) {
        toast("이미지는 5MB 이하로 선택해 주세요.");
        event.target.value = "";
        return;
      }
      const reader = new FileReader();
      reader.onload = () => { $("#product-image-url").value = reader.result; };
      reader.readAsDataURL(file);
    };
  }
  setTimeout(() => $("#editor-fields input, #editor-fields select, #editor-fields textarea")?.focus(), 0);
  $("#editor-form").onsubmit = async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    if (type === "product" && !isLinkImport) data.is_active = Boolean(data.is_active);
    const submit = event.target.querySelector('button[type="submit"]');
    const originalLabel = submit.textContent;
    submit.disabled = true;
    submit.textContent = isLinkImport ? "상품 가져오는 중..." : "저장 중...";
    try {
      const endpoint = type === "category"
        ? "/api/admin/categories"
        : isLinkImport ? "/api/admin/products/import" : "/api/admin/products";
      const result = await api(endpoint, { method: "POST", body: JSON.stringify(data) });
      closeEditor();
      await loadAdmin();
      await loadCatalog();
      toast(isLinkImport
        ? `상품 ${result.total}개를 가져왔습니다. (신규 ${result.inserted}개 · 갱신 ${result.updated}개)`
        : "저장했습니다.");
    } catch (error) {
      toast(error.message);
    } finally {
      submit.disabled = false;
      submit.textContent = originalLabel;
    }
  };
}

async function saveChannels(event) {
  event.preventDefault();
  try {
    const data = await api("/api/admin/channels", { method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(event.target))) });
    fillChannelForm(data.channels);
    cacheChannelForm(data.channels);
    toast("Discord 채널을 저장했습니다.");
  } catch (error) {
    toast(error.message);
  }
}

async function syncProducts() {
  if (!confirm("비비빈스와 일렉샵의 기기 상품을 가져올까요?\n직접 설정한 판매 가격은 변경되지 않습니다. 새로 발견한 상품만 원가 + 3,000원의 임시 가격으로 추가됩니다.")) return;
  const button = $("#sync-products");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "상품 확인 중...";
  try {
    const result = await api("/api/admin/products/sync", { method: "POST", body: "{}" });
    await Promise.all([loadAdmin(), loadCatalog()]);
    const warningCount = Object.keys(result.warnings || {}).length;
    toast(`상품 ${result.total}개 확인 · 신규 ${result.inserted}개 · 갱신 ${result.updated}개${result.unknown_options ? ` · 색상 확인 필요 ${result.unknown_options}개` : ""}${warningCount ? ` · 일부 쇼핑몰 확인 실패 ${warningCount}곳` : ""}`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function showDiscordNotice() {
  const dismissedUntil = Number(localStorage.getItem("discordNoticeDismissedUntil") || 0);
  if (Date.now() < dismissedUntil) return;
  $("#discord-notice").hidden = false;
  document.body.classList.add("modal-open");
}

function closeDiscordNotice() {
  $("#discord-notice").hidden = true;
  document.body.classList.remove("modal-open");
}

function dismissDiscordNoticeForDay() {
  localStorage.setItem("discordNoticeDismissedUntil", String(Date.now() + 24 * 60 * 60 * 1000));
  closeDiscordNotice();
}

boot();
