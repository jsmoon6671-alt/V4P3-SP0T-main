const state = {
  me: null,
  csrf: "",
  products: [],
  cart: [],
  selected: new Set(),
  activeCategory: "전체",
  currentOrder: null,
  timer: null,
  storeResults: [],
  adminCategories: [],
  adminProducts: [],
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[char]);
const productOptions = product => Array.isArray(product.options) ? product.options : [];
const CHANNEL_CACHE_KEY = "v4p3DiscordChannelIds";

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
  $$(".page").forEach(page => page.classList.toggle("active", page.id === id));
  if (id === "account" && state.me) loadAccount();
  if (id === "cart" && state.me) {
    loadCart();
    loadCheckoutCustomer();
  }
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
    $("#login-button").textContent = `${data.user.username} · 로그아웃`;
    $("#admin-link").hidden = !data.is_admin;
    $("#login-button").onclick = logout;
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
}

async function logout() {
  await api("/api/logout", { method: "POST", body: "{}" });
  location.reload();
}

async function loadCatalog() {
  const data = await api("/api/catalog");
  state.products = data.products;
  renderTabs();
  renderProducts();
}

function renderTabs() {
  const categories = ["전체", ...new Set(state.products.map(product => product.category_name || "미분류"))];
  $("#category-tabs").innerHTML = categories.map(category => (
    `<button class="${category === state.activeCategory ? "active" : ""}" data-cat="${escapeHtml(category)}">${escapeHtml(category)}</button>`
  )).join("");
  $$("[data-cat]").forEach(button => {
    button.onclick = () => {
      state.activeCategory = button.dataset.cat;
      renderTabs();
      renderProducts();
    };
  });
}

function renderProducts() {
  const rows = state.products.filter(product => (
    state.activeCategory === "전체" || (product.category_name || "미분류") === state.activeCategory
  ));
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
        <small>${escapeHtml(product.category_name || "미분류")}</small>
        <h3>${escapeHtml(product.name)}</h3>
        <p>${escapeHtml(product.description)}</p>
        ${optionSelect}
        <div class="product-foot">
          <div><strong>${money(product.price)}</strong><br><small>재고 ${product.stock}</small></div>
          <button type="button" aria-label="장바구니에 담기" data-add="${product.id}" ${product.stock < 1 ? "disabled" : ""}>+</button>
        </div>
      </div>
    </article>`;
  }).join("") : '<p class="empty">등록된 상품이 없습니다.</p>';
  $$("[data-add]").forEach(button => {
    button.onclick = () => addCart(Number(button.dataset.add));
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
      <input type="checkbox" aria-label="상품 선택" data-select="${item.product_id}" ${state.selected.has(Number(item.product_id)) ? "checked" : ""}>
      <div class="grow"><strong>${escapeHtml(item.name)}</strong><br><small>${money(item.price)} · 재고 ${item.stock}</small>${optionField}</div>
      <input type="number" aria-label="수량" min="1" max="${item.stock}" value="${item.quantity}" data-qty="${item.product_id}">
      <button type="button" class="remove" data-remove="${item.product_id}">삭제</button>
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
  const subtotal = selectedRows.reduce((sum, item) => sum + Number(item.price) * Number(item.quantity), 0);
  const total = selectedRows.length ? subtotal + 3000 : 0;
  $("#cart-subtotal").textContent = money(subtotal);
  $("#cart-total").textContent = money(total);
  $("#selected-count").textContent = `${selectedRows.length}개 선택`;
}

async function loadCheckoutCustomer() {
  if (!state.me) return;
  try {
    const data = await api("/api/customer");
    for (const key of ["name", "contact", "address", "cvs"]) {
      const input = $(`#checkout-form [name="${key}"]`);
      if (input && !input.value) input.value = data.customer[key] || "";
    }
  } catch (error) {
    toast(error.message);
  }
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
    $("#point-balance").textContent = `${Number(customer.points).toLocaleString()}P`;
    $("#orders").innerHTML = orders.orders.length ? orders.orders.map(order => `<article class="order">
      <div class="order-head"><strong>${escapeHtml(order.order_id)}</strong><span class="badge ${order.status}">${statusText(order.status)}</span></div>
      <p>${escapeHtml(order.product)}</p>
      <small>${escapeHtml(order.amount)} · ${String(order.created_at).slice(0, 16).replace("T", " ")}</small>
    </article>`).join("") : '<div class="empty">아직 주문내역이 없습니다.</div>';
  } catch (error) {
    if (error.message.includes("로그인")) location.href = "/auth/login";
    else toast(error.message);
  }
}

const statusText = status => ({
  PENDING: "입금 대기", APPROVED: "구매 완료", CANCELLED: "자동 취소", REJECTED: "거절",
})[status] || status;

function bindForms() {
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
  $("#checkout-form").onsubmit = checkout;
  $("#channel-form").onsubmit = saveChannels;
  $("#shipping-method").onchange = updateShippingMode;
  $("#store-search").onclick = searchStores;
  $("#store-results").onchange = chooseStore;
  $("#add-category").onclick = () => openEditor("category");
  $("#add-product").onclick = () => openEditor("product");
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

async function checkout(event) {
  event.preventDefault();
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  const form = Object.fromEntries(new FormData(event.target));
  form.adult_confirmed = Boolean(form.adult_confirmed);
  form.product_ids = [...state.selected];
  try {
    const data = await api("/api/checkout", { method: "POST", body: JSON.stringify(form) });
    state.currentOrder = data.order_id;
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
    $("#payment-status").textContent = "결제가 승인되었습니다.";
    return;
  }
  state.timer = setInterval(async () => {
    left = Math.max(0, left - 1);
    draw();
    if (left % 2 === 0 || left === 0) {
      try {
        const order = await api(`/api/orders/${state.currentOrder}`);
        if (order.status === "APPROVED") {
          $("#payment-status").textContent = "입금이 확인되어 자동 승인되었습니다.";
          clearInterval(state.timer);
        } else if (order.status === "CANCELLED") {
          $("#payment-status").textContent = "5분 안에 입금이 확인되지 않아 주문이 취소되었습니다.";
          clearInterval(state.timer);
        }
      } catch {}
    }
  }, 1000);
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
  $("#category-admin").innerHTML = state.adminCategories.map(category => `<div class="admin-row"><span>${escapeHtml(category.name)}</span><span><button class="text-button" data-edit-cat="${category.id}">수정</button> <button class="remove" data-del-cat="${category.id}">삭제</button></span></div>`).join("");
  $("#product-admin").innerHTML = state.adminProducts.map(product => {
    const options = productOptions(product);
    return `<div class="admin-row"><span><b>${escapeHtml(product.name)}</b><br>${escapeHtml(product.category_name || "미분류")} · ${money(product.price)} · ${product.stock}개${options.length ? `<br><small>${escapeHtml(product.option_label)}: ${options.map(escapeHtml).join(", ")}</small>` : ""}</span><button class="text-button" data-edit-product="${product.id}">수정</button></div>`;
  }).join("");
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
}

function closeEditor() {
  $("#editor").hidden = true;
  document.body.classList.remove("modal-open");
}

function openEditor(type, item = {}) {
  const fields = $("#editor-fields");
  $("#editor-title").textContent = type === "category" ? "카테고리 설정" : "상품 설정";
  if (type === "category") {
    fields.innerHTML = `<input type="hidden" name="id" value="${item.id || ""}">
      <label>이름<input name="name" value="${escapeHtml(item.name || "")}" required></label>
      <label>정렬 순서<input name="sort_order" type="number" value="${item.sort_order || 0}"></label>`;
  } else {
    fields.innerHTML = `<input type="hidden" name="id" value="${item.id || ""}">
      <label>카테고리
        <select name="category_id" required>
          <option value="">카테고리를 먼저 선택해 주세요</option>
          ${state.adminCategories.map(category => `<option value="${category.id}" ${category.id === item.category_id ? "selected" : ""}>${escapeHtml(category.name)}</option>`).join("")}
        </select>
      </label>
      <label>상품명<input name="name" value="${escapeHtml(item.name || "")}" required></label>
      <label>상품 이미지 URL<input id="product-image-url" name="image_url" value="${escapeHtml(item.image_url || "")}" placeholder="이미지 주소 또는 아래 파일 선택" required></label>
      <label>이미지 파일<input id="product-image-file" type="file" accept="image/*"></label>
      <label>설명<textarea name="description">${escapeHtml(item.description || "")}</textarea></label>
      <label>가격<input name="price" type="number" min="0" value="${item.price || 0}" required></label>
      <label>재고<input name="stock" type="number" min="0" value="${item.stock || 0}" required></label>
      <label>옵션 종류
        <select name="option_label" required><option value="색상" ${item.option_label !== "맛" ? "selected" : ""}>색상</option><option value="맛" ${item.option_label === "맛" ? "selected" : ""}>맛</option></select>
      </label>
      <label>색상 또는 맛 목록<textarea name="options" placeholder="한 줄에 하나씩 또는 쉼표로 여러 개 입력" required>${escapeHtml(productOptions(item).join("\n"))}</textarea></label>
      <label class="check"><input name="is_active" type="checkbox" ${item.is_active !== false ? "checked" : ""}> 판매 활성화</label>`;
  }
  $("#editor").hidden = false;
  document.body.classList.add("modal-open");
  if (type === "product") {
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
    if (type === "product") data.is_active = Boolean(data.is_active);
    try {
      await api(`/api/admin/${type === "category" ? "categories" : "products"}`, { method: "POST", body: JSON.stringify(data) });
      closeEditor();
      await loadAdmin();
      await loadCatalog();
      toast("저장했습니다.");
    } catch (error) {
      toast(error.message);
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
