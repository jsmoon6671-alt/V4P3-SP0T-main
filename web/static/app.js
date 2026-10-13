const state = {
  me: null,
  isAdmin: false,
  isSubadmin: false,
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
  profileStoreResults: [],
  adminOrders: [],
  adminOrderFilter: "all",
  blacklist: [],
  adminUsers: [],
  adminCouponEvents: [],
  productSearch: "",
  siteMessages: [],
  coupons: [],
  supportType: "general",
  couponEvents: [],
  eventNotices: [],
  notices: [],
  welcomePending: false,
  chatSessions: [],
  activeChatId: null,
  chatPoll: null,
  adminInquiries: [],
  subadmins: [],
  subadminProducts: [],
  subadminOrders: [],
  subadminOrderFilter: "all",
  productEditorScope: "admin",
  directDmUserId: null,
  directDmPoll: null,
  reviewOrderId: null,
  reviewOrderProduct: "",
  routeLoadToken: 0,
  cartLoadedAt: 0,
  cartPromise: null,
  productRenderFrame: null,
  productSearchTimer: null,
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[char]);
const productOptions = product => Array.isArray(product.options) ? product.options : [];
const parseJsonArray = value => {
  if (Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
};
const productOptionGroups = product => {
  const groups = parseJsonArray(product.option_groups_json)
    .filter(group => group && group.label && (group.type === "text" || (Array.isArray(group.options) && group.options.length)));
  if (groups.length) return groups.slice(0, 3);
  const options = productOptions(product);
  return options.length ? [{ label: product.option_label || "옵션", options }] : [];
};
const selectedProductOptions = item => {
  const selected = parseJsonArray(item.selected_options_json);
  return selected.length ? selected : (item.selected_option ? [item.selected_option] : []);
};
const optionSurcharge = option => [...String(option || "").matchAll(/\(\+\s*([\d,]+)\s*(?:원)?\)/g)]
  .reduce((sum, match) => sum + Number(match[1].replaceAll(",", "")), 0);
const productSelectionSurcharge = (groups, options) => options.reduce((sum, option, index) => sum + (groups[index]?.type === "text" ? 0 : optionSurcharge(option)), 0);
const itemUnitPrice = item => Number(item.price || 0) + productSelectionSurcharge(productOptionGroups(item), selectedProductOptions(item));
const BUILTIN_OPTION_LABELS = ["없음", "옵션", "색상", "맛", "농도", "패키지"];
const CUSTOM_OPTION_LABEL = "__custom__";
const TEXT_OPTION_LABEL = "__buyer_text__";
const optionTypeChoices = (selected, isText = false) => {
  const custom = selected !== "없음" && !BUILTIN_OPTION_LABELS.includes(selected);
  return BUILTIN_OPTION_LABELS
    .map(label => `<option value="${label}" ${!isText && label === selected ? "selected" : ""}>${label}</option>`).join("")
    + `<option value="${CUSTOM_OPTION_LABEL}" ${!isText && custom ? "selected" : ""}>옵션명 직접입력</option>`
    + `<option value="${TEXT_OPTION_LABEL}" ${isText ? "selected" : ""}>구매자 직접입력 질문</option>`;
};
const optionGroupEditors = product => {
  const groups = productOptionGroups(product || {});
  return Array.from({ length: 3 }, (_, index) => {
    const group = groups[index] || { label: "없음", options: [] };
    const isText = group.type === "text";
    const customLabel = !isText && !BUILTIN_OPTION_LABELS.includes(group.label) ? group.label : "";
    return `<div class="option-group-editor" data-option-group>
      <strong>선택 항목 ${index + 1}</strong>
      <label>종류<select data-group-label>${optionTypeChoices(group.label, isText)}</select></label>
      <label data-group-custom ${customLabel ? "" : "hidden"}>직접 입력할 옵션명<input data-group-custom-label maxlength="30" value="${escapeHtml(customLabel)}" placeholder="예: 용량, 코일, 구성"></label>
      <label data-group-question ${isText ? "" : "hidden"}>구매자에게 보여줄 질문<input data-group-question-value maxlength="100" value="${isText ? escapeHtml(group.label) : ""}" placeholder="예: 각인할 문구를 입력해 주세요"></label>
      <label data-group-options ${isText ? "hidden" : ""}>목록<textarea data-group-values placeholder="한 줄에 하나씩 입력&#10;예: 3mg&#10;6mg (+1000)">${escapeHtml((group.options || []).join("\n"))}</textarea></label>
    </div>`;
  }).join("");
};
const CHANNEL_CACHE_KEY = "v4p3DiscordChannelIds";
const SHIPPING_BANNER_KEY = "v4p3ShippingBannerClosed";
const NOTICE_SEEN_KEY = "v4p3NoticeSeenAt";
const EVENT_SEEN_KEY = "v4p3EventSeenAt";
const PAGE_PATHS = {
  home: "/",
  events: "/events",
  notices: "/notices",
  support: "/support",
  account: "/account",
  "review-write": "/review-write",
  ranking: "/ranking",
  tiers: "/tiers",
  terms: "/terms",
  tracking: "/tracking",
  cart: "/cart",
  complete: "/cart#complete",
  admin: "/admin",
  "subadmin-panel": "/subadmin-panel",
};

function currentPageId() {
  const hashId = location.hash.slice(1);
  if (hashId && document.getElementById(hashId)?.classList.contains("page")) return hashId;
  return document.body.dataset.page || "home";
}

function goToPage(pageId) {
  const destination = PAGE_PATHS[pageId] || "/";
  if (location.pathname + location.hash === destination) return route();
  location.href = destination;
}

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
  const legacyHash = location.hash.slice(1);
  if (legacyHash && !document.getElementById(legacyHash) && PAGE_PATHS[legacyHash]) {
    location.replace(PAGE_PATHS[legacyHash]);
    return;
  }
  const id = currentPageId();
  if (["account", "review-write", "support", "events", "cart", "complete", "tracking", "admin", "subadmin-panel"].includes(id) && !state.me) {
    try { sessionStorage.setItem("v4p3PostLoginHash", PAGE_PATHS[id]); } catch {}
    location.href = "/auth/login";
    return;
  }
  $$(".page").forEach(page => page.classList.toggle("active", page.id === id));
  const currentPath = location.pathname === "/index.html" ? "/" : location.pathname;
  $$("#nav a").forEach(link => {
    const targetPath = new URL(link.href, location.origin).pathname;
    link.classList.toggle("active", targetPath === currentPath);
  });
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  const loadToken = ++state.routeLoadToken;
  requestAnimationFrame(() => {
    if (loadToken !== state.routeLoadToken || currentPageId() !== id) return;
    if (id === "account" && state.me) loadAccount();
    if (id === "cart" && state.me) {
      loadCart(false);
      loadCheckoutCustomer();
    }
    if (id === "tracking" && state.me) loadCarriers().then(applyTrackingPreset);
    if (id === "tiers") renderTiers();
    if (id === "terms") loadPolicies();
    if (id === "ranking") loadPurchaseRanking();
    if (id === "support") { renderSupportForm(); loadInquiries(); }
    if (id === "events" && state.me) loadCouponEvents(false);
    if (id === "review-write" && state.me) restoreOrderReviewContext();
    if (id === "notices") loadNoticesPage();
    if (id === "subadmin-panel") openSubadminPanel();
    if (id === "admin") openAdminPanel();
  });
}

async function boot() {
  await ensureAgeVerified();
  addEventListener("hashchange", route);
  bindForms();
  try {
    const data = await api("/api/me");
    state.me = data.user;
    state.isAdmin = Boolean(data.is_admin);
    state.isSubadmin = Boolean(data.is_subadmin);
    state.csrf = data.csrf;
    if (data.blocked) {
      $("#blocked-reason").textContent = `등록 사유: ${data.blocked.reason}`;
      $("#blocked-screen").hidden = false;
      document.body.classList.add("modal-open");
      return;
    }
    $("#login-button").textContent = "내 정보";
    $("#admin-link").hidden = !data.is_admin;
    $("#subadmin-panel-link").hidden = !data.is_subadmin;
    if ($("#admin")) $("#admin").hidden = !data.is_admin;
    if ($("#subadmin-panel")) $("#subadmin-panel").hidden = !data.is_subadmin;
    $("#login-button").onclick = () => goToPage("account");
    await loadCart();
    await refreshRegistrationPrompt();
    try { await loadCouponEvents(true); } catch (error) { toast(error.message); }
    try {
      const postLoginHash = sessionStorage.getItem("v4p3PostLoginHash");
      if (postLoginHash) {
        sessionStorage.removeItem("v4p3PostLoginHash");
        location.href = postLoginHash;
      }
    } catch {}
    if (currentPageId() === "cart") await loadCheckoutCustomer();
  } catch {
    $("#login-button").onclick = () => { location.href = "/auth/login"; };
  }
  if (currentPageId() === "home") {
    try {
      await loadCatalog();
    } catch (error) {
      toast(error.message);
    }
  }
  try { await refreshNoticeIndicators(); } catch (error) { console.error("새 공지 표시를 확인하지 못했습니다.", error); }
  route();
  if (!state.welcomePending) loadSiteMessages().catch(() => showDiscordNotice());
  if (currentPageId() === "home") {
    loadReviews().catch(error => {
      const track = $("#review-track");
      if (track) track.innerHTML = '<p class="review-empty">구매 후기를 불러오지 못했습니다. 잠시 후 새로고침해 주세요.</p>';
      console.error("구매후기를 불러오지 못했습니다.", error);
    });
  }
}

async function ensureAgeVerified() {
  const modal = $("#age-gate");
  const button = $("#age-confirm");
  let verified = false;
  try {
    const result = await api("/api/age-verification");
    verified = Boolean(result.verified);
  } catch (error) {
    console.error("성인 확인 상태를 불러오지 못했습니다.", error);
  }
  if (verified) return;

  modal.hidden = false;
  document.body.classList.add("modal-open");
  $$('header, main, #review-showcase').forEach(element => { element.inert = true; });
  await new Promise(resolve => {
    button.onclick = async () => {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "확인 중...";
      try {
        await api("/api/age-verification", { method: "POST", body: "{}" });
        modal.hidden = true;
        document.body.classList.remove("modal-open");
        $$('header, main, #review-showcase').forEach(element => { element.inert = false; });
        resolve();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
        button.textContent = label;
      }
    };
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
    Number(review.rating) >= 1 && Number(review.rating) <= 5
    && String(review.content || "").trim().length >= 5
  ));
  if (!reviews.length) {
    track.innerHTML = '<p class="review-empty">아직 공개할 수 있는 구매후기가 없습니다.</p>';
    return;
  }

  const minimumCards = 6;
  const repeats = Math.max(1, Math.ceil(minimumCards / reviews.length));
  const loopReviews = Array.from({ length: repeats }, () => reviews).flat();
  const cards = loopReviews.map(review => {
    const imageUrl = String(review.image_url || "").trim();
    const rating = Math.max(1, Math.min(5, Number(review.rating) || 5));
    return `
    <article class="review-card${imageUrl ? "" : " review-card-text"}">
      ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="구매후기 사진" loading="lazy">` : ""}
      <div class="review-card-body">
        <div class="review-stars" aria-label="별점 ${rating}점">${"★".repeat(rating)}${"☆".repeat(5 - rating)}</div>
        <p>${escapeHtml(String(review.content).trim())}</p>
      </div>
    </article>`;
  }).join("");

  track.style.setProperty("--review-duration", `${Math.max(30, loopReviews.length * 7)}s`);
  track.innerHTML = `<div class="review-group">${cards}</div><div class="review-group" aria-hidden="true">${cards}</div>`;
  section.hidden = false;
}

function renderTabs() {
  if (!$("#parent-category-tabs") || !$("#category-tabs")) return;
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
      scheduleProductRender();
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
      scheduleProductRender();
    };
  });
}

function renderProducts() {
  if (!$("#products")) return;
  const rows = state.products.filter(product => {
    const query = state.productSearch.trim().toLocaleLowerCase("ko");
    if (query && !`${product.name} ${product.description || ""} ${product.category_name || ""}`.toLocaleLowerCase("ko").includes(query)) return false;
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
    const optionGroups = productOptionGroups(product);
    const optionSelect = optionGroups.map((group, index) => group.type === "text" ? `
      <label class="product-option product-question${index ? " locked" : ""}">${escapeHtml(group.label)}
        <textarea data-product-option="${product.id}" data-option-index="${index}" maxlength="500" rows="2" placeholder="답변을 입력해 주세요" ${index ? "disabled" : ""}></textarea>
      </label>` : `
      <label class="product-option${index ? " locked" : ""}">${escapeHtml(group.label)}
        <select data-product-option="${product.id}" data-option-index="${index}" ${index ? "disabled" : ""}>
          <option value="">${index ? "위 옵션을 먼저 선택해 주세요" : "선택해 주세요"}</option>
          ${group.options.map(option => `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`).join("")}
        </select>
      </label>`).join("");
    return `<article class="product">
      <div class="product-media">${product.image_url ? `<img src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.name)}" loading="lazy" decoding="async">` : "<span>V</span>"}</div>
      <div class="product-body">
        <small>${escapeHtml(product.parent_category_name ? `${product.parent_category_name} · ${product.category_name}` : (product.category_name || "미분류"))}</small>
        <h3>${escapeHtml(product.name)}</h3>
        <p class="product-description">${escapeHtml(product.description)}</p>
        ${Number(product.free_shipping_quantity || 0) > 0 ? `<p class="product-shipping-offer">이 상품만 ${Number(product.free_shipping_quantity)}개 이상 구매 시 무료배송</p>` : ""}
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
  $$("[data-product-option]").forEach(field => {
    const updateProductOptions = () => {
      const product = state.products.find(item => Number(item.id) === Number(field.dataset.productOption));
      const optionGroups = productOptionGroups(product || {});
      const price = $(`[data-product-price="${field.dataset.productOption}"]`);
      const optionFields = $$(`[data-product-option="${field.dataset.productOption}"]`);
      optionFields.forEach((field, index) => {
        const enabled = index === 0 || Boolean(optionFields[index - 1].value);
        field.disabled = !enabled;
        if (!enabled) field.value = "";
        field.closest(".product-option")?.classList.toggle("locked", !enabled);
      });
      const selected = optionFields.map(field => field.value);
      if (product && price) price.textContent = money(Number(product.price) + productSelectionSurcharge(optionGroups, selected));
    };
    field.onchange = updateProductOptions;
    if (field.tagName === "TEXTAREA") field.oninput = updateProductOptions;
  });
}

function scheduleProductRender() {
  if (state.productRenderFrame) cancelAnimationFrame(state.productRenderFrame);
  state.productRenderFrame = requestAnimationFrame(() => {
    state.productRenderFrame = null;
    renderProducts();
  });
}

async function addCart(productId) {
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  const product = state.products.find(item => Number(item.id) === productId);
  const optionGroups = productOptionGroups(product || {});
  const selectedOptions = $$(`[data-product-option="${productId}"]`).map(select => select.value);
  const missingIndex = selectedOptions.findIndex(value => !value.trim());
  if (optionGroups.length && missingIndex >= 0) {
    toast(optionGroups[missingIndex].type === "text" ? `${optionGroups[missingIndex].label} 질문에 답변해 주세요.` : `${optionGroups[missingIndex].label}을 선택해 주세요.`);
    return;
  }
  try {
    await api("/api/cart", {
      method: "POST",
      body: JSON.stringify({ product_id: productId, quantity: 1, selected_options: selectedOptions }),
    });
    toast("장바구니에 담았습니다.");
    await loadCart();
  } catch (error) {
    toast(error.message);
  }
}

async function loadCart(force = true) {
  if (!state.me) return;
  if (!force && state.cartLoadedAt && Date.now() - state.cartLoadedAt < 5000) {
    renderCart();
    return;
  }
  if (state.cartPromise) return state.cartPromise;
  state.cartPromise = (async () => {
    const data = await api("/api/cart");
    state.cart = data.items;
    state.selected = new Set(data.items.map(item => Number(item.product_id)));
    state.cartLoadedAt = Date.now();
    $("#cart-count").textContent = data.items.length;
    renderCart();
  })();
  try {
    await state.cartPromise;
  } finally {
    state.cartPromise = null;
  }
}

function renderCart() {
  const box = $("#cart-items");
  if (!box) return;
  box.innerHTML = state.cart.length ? state.cart.map(item => {
    const optionGroups = productOptionGroups(item);
    const selectedOptions = selectedProductOptions(item);
    const optionField = optionGroups.map((group, index) => group.type === "text" ? `<label class="cart-option-label">${escapeHtml(group.label)}
      <textarea data-cart-option="${item.product_id}" data-option-index="${index}" maxlength="500" rows="2">${escapeHtml(selectedOptions[index] || "")}</textarea></label>` : `<label class="cart-option-label">${escapeHtml(group.label)}
      <select data-cart-option="${item.product_id}" data-option-index="${index}">
        ${group.options.map(option => `<option value="${escapeHtml(option)}" ${option === selectedOptions[index] ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
      </select></label>`).join("");
    const surcharge = productSelectionSurcharge(optionGroups, selectedOptions);
    return `<div class="cart-row">
      <input class="cart-check" type="checkbox" aria-label="상품 선택" data-select="${item.product_id}" ${state.selected.has(Number(item.product_id)) ? "checked" : ""}>
      <div class="grow"><strong>${escapeHtml(item.name)}</strong><br><small>${money(itemUnitPrice(item))}${surcharge ? ` · 옵션 +${money(surcharge)}` : ""}</small>${Number(item.free_shipping_quantity || 0) > 0 ? `<small class="cart-shipping-offer">단독 ${Number(item.free_shipping_quantity)}개 이상 무료배송</small>` : ""}${optionField}</div>
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
  $$("[data-cart-option]", box).forEach(field => {
    field.onchange = async () => {
      const item = state.cart.find(row => Number(row.product_id) === Number(field.dataset.cartOption));
      const selectedOptions = $$(`[data-cart-option="${field.dataset.cartOption}"]`, box).map(optionField => optionField.value);
      await saveCartRow(Number(field.dataset.cartOption), Number(item.quantity), selectedOptions);
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

async function saveCartRow(productId, quantity, selectedOptions = null) {
  const item = state.cart.find(row => Number(row.product_id) === productId);
  try {
    await api("/api/cart", {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        quantity,
        selected_options: selectedOptions ?? selectedProductOptions(item),
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
  const productFreeShipping = qualifiesForProductFreeShipping(selectedRows);
  const shipping = selectedRows.length ? (productFreeShipping ? 0 : shippingFee(subtotal)) : 0;
  const beforeDiscount = subtotal + shipping;
  const discount = checkoutPointDiscount(beforeDiscount);
  const coupon = state.coupons.find(item => item.code === $("#checkout-coupon")?.value);
  const couponEligible = !coupon || subtotal >= Number(coupon.min_order_amount || 0);
  const couponDiscount = couponEligible ? Math.min(Number(coupon?.discount_amount || 0), Math.max(0, beforeDiscount - discount)) : 0;
  const total = Math.max(0, beforeDiscount - discount - couponDiscount);
  $("#cart-subtotal").textContent = money(subtotal);
  $("#cart-shipping").textContent = money(shipping);
  $("#cart-discount").textContent = `-${money(discount)}`;
  $("#cart-discount-row").hidden = discount === 0;
  $("#cart-coupon-discount").textContent = `-${money(couponDiscount)}`;
  $("#cart-coupon-row").hidden = couponDiscount === 0;
  $("#cart-total").textContent = money(total);
  const couponHint = $("#coupon-use-hint");
  if (couponHint) couponHint.textContent = coupon
    ? (couponEligible ? `${money(coupon.discount_amount)} 할인이 적용됩니다.` : `이 쿠폰은 상품 금액 ${money(coupon.min_order_amount)} 이상 구매해야 사용할 수 있습니다.`)
    : "쿠폰별 최소 주문금액 조건이 적용됩니다.";
  $("#selected-count").textContent = `${selectedRows.length}개 선택`;
  updateShippingBenefit(subtotal, selectedRows.length > 0, productFreeShipping);
  updatePointUseHint(beforeDiscount);
}

function shippingFee(subtotal) {
  const amount = Math.max(0, Number(subtotal) || 0);
  if (amount >= 50000) return 0;
  if (amount >= 35000) return 1500;
  return amount > 0 ? 3000 : 0;
}

function qualifiesForProductFreeShipping(items) {
  if (items.length !== 1) return false;
  const threshold = Number(items[0].free_shipping_quantity || 0);
  return threshold > 0 && Number(items[0].quantity || 0) >= threshold;
}

function updateShippingBenefit(subtotal, hasItems, productFreeShipping = false) {
  const message = $("#shipping-benefit");
  if (!message) return;
  if (!hasItems) {
    message.textContent = "선택한 상품 금액에 따라 자동 적용됩니다.";
  } else if (productFreeShipping) {
    message.textContent = "단독 상품 수량 무료배송이 적용되었습니다.";
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
    const [data, couponData] = await Promise.all([api("/api/customer"), api("/api/coupons")]);
    for (const key of ["name", "contact", "address", "cvs"]) {
      const input = $(`#checkout-form [name="${key}"]`);
      if (input && !input.value) input.value = data.customer[key] || "";
    }
    state.pointBalance = Number(data.points || 0);
    state.coupons = couponData.coupons || [];
    $("#checkout-coupon").innerHTML = '<option value="">사용 안 함</option>' + state.coupons.map(coupon => `<option value="${escapeHtml(coupon.code)}">${escapeHtml(coupon.code)} · ${money(coupon.discount_amount)} 할인${Number(coupon.min_order_amount || 0) ? ` · ${money(coupon.min_order_amount)} 이상` : ""}</option>`).join("");
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
  const beforeCoupon = rows.length ? subtotal + shippingFee(subtotal) : 0;
  const coupon = state.coupons.find(item => item.code === $("#checkout-coupon")?.value);
  const couponDiscount = coupon && subtotal >= Number(coupon.min_order_amount || 0) ? Number(coupon.discount_amount || 0) : 0;
  return Math.max(0, beforeCoupon - couponDiscount);
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

async function searchProfileStores() {
  const brand = $("#profile-cvs-brand").value;
  const query = $("#profile-store-query").value.trim();
  if (!brand) return toast("GS25 또는 CU를 먼저 선택해 주세요.");
  if (!query) return toast("편의점 지점명을 입력해 주세요.");
  const button = $("#profile-store-search");
  button.disabled = true;
  button.textContent = "조회 중";
  try {
    const data = await api(`/api/stores?brand=${encodeURIComponent(brand)}&q=${encodeURIComponent(query)}`);
    state.profileStoreResults = data.stores || [];
    $("#profile-store-results").innerHTML = '<option value="">받을 편의점을 선택해 주세요</option>' + state.profileStoreResults.map((store, index) => (
      `<option value="${index}">${escapeHtml(store.name)} · ${escapeHtml(store.address)}</option>`
    )).join("");
    if (!state.profileStoreResults.length) toast("검색된 편의점이 없습니다.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "주소 조회";
  }
}

function chooseProfileStore() {
  const store = state.profileStoreResults[Number($("#profile-store-results").value)];
  if (!store) return;
  $('#customer-form [name="cvs"]').value = store.name;
  $('#customer-form [name="cvs_address"]').value = store.address;
}

async function refreshRegistrationPrompt() {
  const prompt = $("#register-prompt");
  if (!prompt || !state.me) return;
  try {
    const data = await api("/api/customer");
    const customer = data.customer || {};
    const hasDestination = Boolean(String(customer.address || "").trim()) || Boolean(
      String(customer.cvs || "").trim() && String(customer.cvs_address || "").trim()
    );
    const hasRefundAccount = Boolean(String(customer.refund_bank || "").trim() && String(customer.refund_account_number || "").trim() && String(customer.refund_account_holder || "").trim());
    prompt.hidden = Boolean(String(customer.name || "").trim() && String(customer.contact || "").trim() && hasRefundAccount && customer.refund_policy_accepted && hasDestination);
  } catch {
    prompt.hidden = false;
  }
}

function bindStarPicker() {
  const picker = $("#star-picker");
  if (!picker) return;
  picker.innerHTML = Array.from({ length: 5 }, (_, index) => `<button type="button" data-rating="${index + 1}" aria-label="${index + 1}점">★</button>`).join("");
  const input = $('#review-form [name="rating"]');
  const paint = rating => $$('button', picker).forEach(button => button.classList.toggle("active", Number(button.dataset.rating) <= rating));
  $$('button', picker).forEach(button => {
    button.onclick = () => { input.value = button.dataset.rating; paint(Number(input.value)); };
  });
  paint(Number(input.value));
}

const fileToDataUrl = file => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(reader.result);
  reader.onerror = () => reject(new Error("사진을 읽지 못했습니다."));
  reader.readAsDataURL(file);
});

async function submitReview(event) {
  event.preventDefault();
  if (!state.me) return void (location.href = "/auth/login");
  const form = event.currentTarget;
  const files = [...form.elements.images.files];
  if (files.length > 4) return toast("후기 사진은 최대 4장까지 등록할 수 있습니다.");
  if (files.some(file => file.size > 5 * 1024 * 1024)) return toast("후기 사진은 한 장당 5MB 이하로 등록해 주세요.");
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  submit.textContent = "후기 등록 중...";
  try {
    const images = await Promise.all(files.map(fileToDataUrl));
    const data = await api("/api/reviews", {
      method: "POST",
      body: JSON.stringify({
        rating: Number(form.elements.rating.value),
        content: form.elements.content.value,
        images,
        order_id: form.elements.order_id.value,
      }),
    });
    form.reset();
    form.elements.rating.value = "5";
    clearOrderReviewContext();
    bindStarPicker();
    await loadReviews();
    toast(data.reward ? `후기가 등록되어 ${Number(data.reward).toLocaleString()}P가 지급되었습니다.` : "후기가 등록되었습니다.");
    goToPage("home");
  } catch (error) {
    toast(error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = "후기 올리기";
  }
}

async function loadPurchaseRanking() {
  const target = $("#purchase-ranking");
  if (!target) return;
  target.innerHTML = '<div class="empty">랭킹을 불러오는 중입니다.</div>';
  try {
    const data = await api("/api/purchase-ranking");
    target.innerHTML = data.ranking.length ? data.ranking.map((item, index) => `
      <div class="ranking-row"><span class="ranking-number">${index + 1}</span><strong>${escapeHtml(item.name)}</strong><b>${money(item.total)}</b></div>
    `).join("") : '<div class="empty">아직 구매랭킹이 없습니다.</div>';
  } catch (error) {
    target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

async function loadAccount() {
  if (!state.me) return;
  try {
    const [customer, orders] = await Promise.all([api("/api/customer"), api("/api/orders")]);
    Object.entries(customer.customer).forEach(([key, value]) => {
      if (["refund_policy_accepted", "refund_policy_version"].includes(key)) return;
      const input = $(`#customer-form [name="${key}"]`);
      if (input) input.value = value || "";
    });
    setupRefundPolicy(customer.refund_policy || {});
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
      ${(order.waybill_number || (order.fulfillment_status === "DELIVERED" && !order.reviewed)) ? `<div class="order-actions">${order.waybill_number ? `<span>운송장 ${escapeHtml(order.waybill_number)}</span><button type="button" class="text-button" data-order-detail="${escapeHtml(order.order_id)}">상세조회</button>` : ""}${order.fulfillment_status === "DELIVERED" && !order.reviewed ? `<button type="button" class="button compact order-review-button" data-order-review="${escapeHtml(order.order_id)}" data-order-product="${escapeHtml(order.product)}">후기작성</button>` : ""}</div>${order.waybill_number ? `<div class="order-detail" data-order-detail-box="${escapeHtml(order.order_id)}" hidden></div>` : ""}` : ""}
    </article>`;
    }).join("") : '<div class="empty">아직 주문내역이 없습니다.</div>';
    $$("[data-order-detail]").forEach(button => {
      button.onclick = () => toggleOrderTracking(button);
    });
    $$("[data-order-review]").forEach(button => {
      button.onclick = () => beginOrderReview(button.dataset.orderReview, button.dataset.orderProduct);
    });
  } catch (error) {
    if (error.message.includes("로그인")) location.href = "/auth/login";
    else toast(error.message);
  }
}

function setRefundAccountFieldsEnabled(enabled) {
  $$("#refund-account-fields input").forEach(input => { input.disabled = !enabled; });
  $("#refund-account-fields").classList.toggle("locked", !enabled);
}

function setupRefundPolicy(policy) {
  const scrollBox = $("#refund-policy-scroll");
  const checkbox = $("#refund-policy-accepted");
  const hint = $("#refund-policy-scroll-hint");
  $("#refund-policy-text").textContent = policy.content || "등록된 환불약관이 없습니다.";
  $("#refund-policy-version").value = policy.version || "";
  scrollBox.scrollTop = 0;
  checkbox.checked = Boolean(policy.accepted);
  checkbox.disabled = !policy.accepted;
  setRefundAccountFieldsEnabled(Boolean(policy.accepted));

  const updateScrollState = () => {
    if (policy.accepted) return;
    const reachedBottom = scrollBox.scrollHeight <= scrollBox.clientHeight + 4
      || scrollBox.scrollTop + scrollBox.clientHeight >= scrollBox.scrollHeight - 4;
    checkbox.disabled = !reachedBottom;
    hint.textContent = reachedBottom ? "약관을 모두 확인했습니다. 동의 항목을 체크해 주세요." : "환불약관을 끝까지 내려 주세요.";
  };
  scrollBox.onscroll = updateScrollState;
  checkbox.onchange = () => setRefundAccountFieldsEnabled(checkbox.checked);
  requestAnimationFrame(updateScrollState);
}

async function loadPolicies() {
  try {
    const data = await api("/api/policies");
    $("#managed-terms-text").textContent = data.terms_of_service || "";
    $("#managed-terms-content").hidden = false;
    $("#default-terms-content").hidden = true;
  } catch (error) {
    $("#managed-terms-content").hidden = true;
    $("#default-terms-content").hidden = false;
  }
}

async function loadAdminPolicies() {
  const form = $("#policy-admin-form");
  try {
    const data = await api("/api/admin/policies");
    form.elements.terms_of_service.value = data.terms_of_service || "";
    form.elements.refund_policy.value = data.refund_policy || "";
  } catch (error) {
    toast(error.message);
  }
}

async function saveAdminPolicies(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter || form.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await api("/api/admin/policies", { method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(form))) });
    await loadAdminPolicies();
    toast("이용약관과 환불약관을 저장했습니다.");
  } catch (error) {
    toast(error.message);
  } finally {
    submit.disabled = false;
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
  PENDING: "입금 대기", APPROVED: "구매 완료", CANCELLED: "주문 취소", REJECTED: "거절",
  SHIPPING_READY: "배송 준비중", SHIPPING: "배송중", DELIVERED: "배송완료",
})[status] || status;

async function openChatPanel() {
  if (!state.me) { location.href = "/auth/login"; return; }
  $("#chat-panel").hidden = false;
  await loadChatSessions();
}

function closeChatPanel() {
  $("#chat-panel").hidden = true;
  clearInterval(state.chatPoll); state.chatPoll = null;
}

async function loadChatSessions() {
  try {
    const data = await api("/api/chat");
    state.chatSessions = data.sessions || [];
    $("#chat-availability").textContent = data.available ? `상담이 가능합니다! · ${data.start}~${data.end}` : `상담시간이 아닙니다. · ${data.start}~${data.end}`;
    $("#chat-start").disabled = !data.available;
    $("#chat-history").innerHTML = state.chatSessions.map(session => `<button type="button" data-chat-session="${session.id}"><span><b>${escapeHtml(session.subject)}</b><small>${new Date(session.updated_at).toLocaleString("ko-KR")}</small></span><i>${({OPEN:"접수",ASSIGNED:"상담중",CLOSING:"종료확인",CLOSED:"종료",FORCE_CLOSED:"강제종료"})[session.status] || session.status}</i></button>`).join("") || '<p class="empty">아직 상담 기록이 없습니다.</p>';
    $$('[data-chat-session]').forEach(button => { button.onclick = () => openChatRoom(button.dataset.chatSession); });
  } catch (error) { toast(error.message); }
}

async function createChatSession() {
  const content = $("#chat-initial-content").value.trim();
  if (!content) return toast("문의 내용을 입력해 주세요.");
  try {
    const data = await api("/api/chat", { method: "POST", body: JSON.stringify({ content }) });
    $("#chat-initial-content").value = ""; $("#chat-initial-count").textContent = "0"; $("#chat-initial-wrap").hidden = true;
    await openChatRoom(data.session.id);
  } catch (error) { toast(error.message); }
}

async function openChatRoom(sessionId) {
  state.activeChatId = String(sessionId);
  $("#chat-home").hidden = true; $("#chat-room").hidden = false;
  await loadChatRoom();
  clearInterval(state.chatPoll);
  state.chatPoll = setInterval(() => { if (!$("#chat-panel").hidden && state.activeChatId) loadChatRoom(true); }, 2500);
}

async function loadChatRoom(silent = false) {
  if (!state.activeChatId) return;
  try {
    const data = await api(`/api/chat/${state.activeChatId}`);
    const box = $("#chat-messages");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    box.innerHTML = (data.messages || []).map(message => `<article class="chat-bubble ${message.sender_type === "USER" ? "user" : message.sender_type === "STAFF" ? "staff" : "system"}"><small>${escapeHtml(message.sender_name || "V4P3 SP0T")}</small>${message.content ? `<p>${escapeHtml(message.content)}</p>` : ""}${message.image_url ? `<img src="${message.image_url}" alt="상담 첨부 이미지">` : ""}${message.sender_type === "CLOSE_PROMPT" ? `<div class="chat-close-actions"><button type="button" class="button danger compact" data-chat-choice="close">종료하기</button><button type="button" class="button secondary compact" data-chat-choice="continue">계속하기</button></div>` : ""}<time>${new Date(message.created_at).toLocaleTimeString("ko-KR", {hour:"2-digit",minute:"2-digit"})}</time></article>`).join("");
    if (nearBottom || !silent) box.scrollTop = box.scrollHeight;
    $$('[data-chat-choice]', box).forEach(button => { button.onclick = () => chatCloseChoice(button.dataset.chatChoice); });
    const writable = ["OPEN", "ASSIGNED", "CLOSING"].includes(data.session.status);
    $("#chat-message-form").hidden = !writable;
  } catch (error) { if (!silent) toast(error.message); }
}

async function sendChatMessage(event) {
  event.preventDefault();
  const form = event.currentTarget; const data = new FormData(form);
  const file = form.elements.image.files?.[0];
  if (file && file.size > 5 * 1024 * 1024) return toast("이미지는 5MB 이하로 보내 주세요.");
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  try {
    const response = await fetch(`/api/chat/${state.activeChatId}/messages`, { method: "POST", headers: {"X-CSRF-Token": state.csrf}, body: data });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "메시지를 보내지 못했습니다.");
    form.reset();
    form.querySelector("textarea").style.height = "44px";
    form.querySelector(".chat-file").classList.remove("selected");
    $("#chat-upload-preview").hidden = true;
    $("#chat-upload-preview img").removeAttribute("src");
    await loadChatRoom();
  } catch (error) { toast(error.message); }
  finally { submit.disabled = false; }
}

async function chatCloseChoice(choice) {
  try { await api(`/api/chat/${state.activeChatId}/close-choice`, { method: "POST", body: JSON.stringify({ choice }) }); await loadChatRoom(); await loadChatSessions(); }
  catch (error) { toast(error.message); }
}

function backToChatHistory() {
  clearInterval(state.chatPoll); state.chatPoll = null; state.activeChatId = null;
  $("#chat-room").hidden = true; $("#chat-home").hidden = false; loadChatSessions();
}

function bindForms() {
  bindShippingBanner();
  restoreChannelForm();
  $$("#channel-form input").forEach(input => {
    input.addEventListener("input", () => cacheChannelForm());
  });
  const customerForm = $("#customer-form");
  if (customerForm) customerForm.onsubmit = async event => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.target));
      data.refund_policy_accepted = event.target.elements.refund_policy_accepted.checked;
      await api("/api/customer", { method: "PUT", body: JSON.stringify(data) });
      toast("고객정보를 저장했습니다.");
      await refreshRegistrationPrompt();
    } catch (error) {
      toast(error.message);
    }
  };
  if ($("#account-logout")) $("#account-logout").onclick = logout;
  if ($("#checkout-form")) $("#checkout-form").onsubmit = checkout;
  if ($("#use-all-points")) $("#use-all-points").onclick = useAllPoints;
  if ($("#checkout-form [name=\"points\"]")) $("#checkout-form [name=\"points\"]").oninput = cartTotal;
  if ($("#checkout-coupon")) $("#checkout-coupon").onchange = cartTotal;
  if ($("#tracking-form")) $("#tracking-form").onsubmit = trackDelivery;
  if ($("#channel-form")) $("#channel-form").onsubmit = saveChannels;
  if ($("#shipping-method")) $("#shipping-method").onchange = updateShippingMode;
  if ($("#store-search")) $("#store-search").onclick = searchStores;
  if ($("#store-results")) $("#store-results").onchange = chooseStore;
  if ($("#profile-store-search")) $("#profile-store-search").onclick = searchProfileStores;
  if ($("#profile-store-results")) $("#profile-store-results").onchange = chooseProfileStore;
  if ($("#profile-cvs-brand")) $("#profile-cvs-brand").onchange = () => {
    state.profileStoreResults = [];
    $("#profile-store-results").innerHTML = '<option value="">편의점을 조회해 주세요</option>';
    $('#customer-form [name="cvs_address"]').value = "";
  };
  if ($("#review-form")) $("#review-form").onsubmit = submitReview;
  bindStarPicker();
  if ($("#add-parent-category")) $("#add-parent-category").onclick = () => openEditor("category", { parentMode: true });
  if ($("#add-category")) $("#add-category").onclick = () => openEditor("category", { childMode: true });
  if ($("#add-manual-product")) $("#add-manual-product").onclick = () => { state.productEditorScope = "admin"; openEditor("product", { manualCreate: true }); };
  if ($("#add-product")) $("#add-product").onclick = () => { state.productEditorScope = "admin"; openEditor("product"); };
  if ($("#sync-products")) $("#sync-products").onclick = syncProducts;
  $("#editor-close").onclick = closeEditor;
  $("#editor").onclick = event => { if (event.target === $("#editor")) closeEditor(); };
  $("#notice-close").onclick = closeDiscordNotice;
  $("#notice-day").onclick = dismissDiscordNoticeForDay;
  $("#action-banner-close").onclick = () => { $("#action-banner").hidden = true; };
  if ($("#refresh-admin-orders")) $("#refresh-admin-orders").onclick = loadAdminOrders;
  if ($("#refresh-blacklist")) $("#refresh-blacklist").onclick = loadBlacklist;
  if ($("#blacklist-add-form")) $("#blacklist-add-form").onsubmit = addBlacklist;
  if ($("#blacklist-remove-form")) $("#blacklist-remove-form").onsubmit = removeBlacklist;
  if ($("#blacklist-type")) $("#blacklist-type").onchange = updateBlacklistDuration;
  if ($("#blacklist-duration")) $("#blacklist-duration").onchange = updateBlacklistDuration;
  if ($("#refresh-admin-users")) $("#refresh-admin-users").onclick = loadAdminUsers;
  if ($("#refresh-admin-inquiries")) $("#refresh-admin-inquiries").onclick = loadAdminInquiries;
  if ($("#refresh-direct-dm")) $("#refresh-direct-dm").onclick = () => loadDirectDm(state.directDmUserId, false);
  if ($("#direct-dm-open-form")) $("#direct-dm-open-form").onsubmit = event => {
    event.preventDefault();
    openDirectDm(String(new FormData(event.currentTarget).get("user_id") || "").trim());
  };
  if ($("#direct-dm-send-form")) $("#direct-dm-send-form").onsubmit = sendDirectDm;
  if ($("#refresh-subadmins")) $("#refresh-subadmins").onclick = loadSubadmins;
  if ($("#subadmin-form")) $("#subadmin-form").onsubmit = saveSubadmin;
  if ($("#refresh-subadmin-panel")) $("#refresh-subadmin-panel").onclick = loadSubadminPanel;
  if ($("#refresh-admin-coupon-events")) $("#refresh-admin-coupon-events").onclick = loadAdminCouponEvents;
  if ($("#point-admin-form")) $("#point-admin-form").onsubmit = saveAdminPoints;
  if ($("#coupon-admin-form")) $("#coupon-admin-form").onsubmit = issueCoupon;
  if ($('#coupon-admin-form [name="all_users"]')) $('#coupon-admin-form [name="all_users"]').onchange = updateCouponRecipient;
  if ($("#coupon-event-admin-form")) $("#coupon-event-admin-form").onsubmit = createCouponEvent;
  if ($("#policy-admin-form")) $("#policy-admin-form").onsubmit = saveAdminPolicies;
  if ($('#coupon-event-admin-form [name="image"]')) $('#coupon-event-admin-form [name="image"]').onchange = previewCouponEventImage;
  $("#claim-welcome-coupon").onclick = () => claimCouponEvent("WELCOME", true);
  $("#delivery-review-later").onclick = closeDeliveredReviewPrompt;
  $("#delivery-review-write").onclick = () => {
    const modal = $("#delivery-review-modal");
    const orderId = modal.dataset.orderId;
    const product = modal.dataset.product;
    closeDeliveredReviewPrompt();
    beginOrderReview(orderId, product);
  };
  $$("[data-support-type]").forEach(button => {
    button.onclick = () => {
      state.supportType = button.dataset.supportType;
      renderSupportForm();
    };
  });
  if ($("#support-form")) $("#support-form").onsubmit = createSupportTicket;
  $("#chat-launcher").onclick = openChatPanel;
  $("#chat-panel-close").onclick = closeChatPanel;
  $("#chat-start").onclick = () => { $("#chat-initial-wrap").hidden = false; $("#chat-initial-content").focus(); };
  $("#chat-create").onclick = createChatSession;
  $("#chat-initial-content").oninput = event => { $("#chat-initial-count").textContent = String(event.target.value.length); };
  $("#chat-back").onclick = backToChatHistory;
  $("#chat-message-form").onsubmit = sendChatMessage;
  const chatMessageInput = $('#chat-message-form textarea[name="content"]');
  chatMessageInput.onkeydown = event => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (chatMessageInput.value.trim() || $('#chat-message-form input[name="image"]').files?.length) {
      $("#chat-message-form").requestSubmit();
    }
  };
  chatMessageInput.oninput = () => {
    chatMessageInput.style.height = "44px";
    chatMessageInput.style.height = `${Math.min(chatMessageInput.scrollHeight, 112)}px`;
  };
  $('#chat-message-form input[name="image"]').onchange = event => {
    const file = event.target.files?.[0];
    event.target.closest(".chat-file").classList.toggle("selected", Boolean(file));
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { toast("이미지는 5MB 이하로 보내 주세요."); event.target.value = ""; return; }
    const preview = $("#chat-upload-preview");
    $("#chat-upload-preview img").src = URL.createObjectURL(file);
    preview.hidden = false;
    $("#chat-message-form").requestSubmit();
  };
  $$(".notice-admin-form").forEach(form => { form.onsubmit = saveNotice; });
  $$('.notice-admin-form input[name="image"]').forEach(input => { input.onchange = previewNoticeImage; });
  $$('.notice-admin-form input[name="remove_image"]').forEach(input => {
    input.onchange = () => {
      const preview = input.closest("form").querySelector(".notice-image-preview");
      if (input.checked) preview.hidden = true;
      else if (preview.dataset.existingSrc) { preview.src = preview.dataset.existingSrc; preview.hidden = false; }
    };
  });
  if ($("#product-search")) $("#product-search").oninput = event => {
    state.productSearch = event.target.value;
    clearTimeout(state.productSearchTimer);
    state.productSearchTimer = setTimeout(scheduleProductRender, 120);
  };
  $("#site-message-close").onclick = showNextSiteMessage;
  $("#site-message-day").onclick = dismissSiteMessageDay;
  document.addEventListener("keydown", event => {
    if (event.key === "F12" || (event.ctrlKey && event.shiftKey && ["I", "J", "C"].includes(event.key.toUpperCase())) || (event.ctrlKey && event.key.toUpperCase() === "U")) event.preventDefault();
  });
  $$('[data-admin-section-target]').forEach(button => {
    button.onclick = () => setAdminSection(button.dataset.adminSectionTarget);
  });
  addEventListener("keydown", event => {
    if (event.key === "Escape" && !$("#editor").hidden) closeEditor();
  });
  if ($("#shipping-method")) updateShippingMode();
  renderSupportForm();
  updateCouponRecipient();
}

function updateCouponRecipient() {
  const form = $("#coupon-admin-form");
  if (!form) return;
  const allUsers = form.elements.all_users.checked;
  form.elements.user_id.disabled = allUsers;
  form.elements.user_id.required = !allUsers;
  if (allUsers) form.elements.user_id.value = "";
}

const SUPPORT_TYPES = {
  general: { title: "일반문의 작성", icon: "💌", fields: [
    { name: "문의내용", label: "문의내용", type: "textarea", placeholder: "문의하실 내용을 자세히 작성해 주세요." },
  ] },
  payment: { title: "입금문의 작성", icon: "💳", fields: [
    { name: "문의내용", label: "문의내용", type: "textarea", placeholder: "입금 또는 결제 관련 문의를 작성해 주세요." },
  ] },
  shipping: { title: "배송문의 작성", icon: "🚚", fields: [
    { name: "문의내용", label: "문의내용", type: "textarea", placeholder: "배송 또는 운송장 관련 문의를 작성해 주세요." },
  ] },
  partner: { title: "제휴문의 작성", icon: "🤝", fields: [
    { name: "문의내용", label: "문의내용", type: "textarea", placeholder: "제휴 문의 내용을 자세히 작성해 주세요." },
  ] },
};

function renderSupportForm() {
  const form = $("#support-form");
  if (!form) return;
  const config = SUPPORT_TYPES[state.supportType] || SUPPORT_TYPES.general;
  form.elements.ticket_type.value = state.supportType;
  $("#support-form-icon").textContent = config.icon;
  $("#support-form-title").textContent = config.title;
  $("#support-fields").innerHTML = config.fields.map(field => `<label>${escapeHtml(field.label)}${field.type === "textarea"
    ? `<textarea name="${escapeHtml(field.name)}" maxlength="1500" placeholder="${escapeHtml(field.placeholder)}" required></textarea>`
    : `<input name="${escapeHtml(field.name)}" maxlength="1500" placeholder="${escapeHtml(field.placeholder)}" required>`}</label>`).join("");
  $$("[data-support-type]").forEach(button => button.classList.toggle("active", button.dataset.supportType === state.supportType));
  $("#support-complete").hidden = true;
}

async function createSupportTicket(event) {
  event.preventDefault();
  if (!state.me) {
    try { sessionStorage.setItem("v4p3PostLoginHash", PAGE_PATHS.support); } catch {}
    location.href = "/auth/login";
    return;
  }
  const submit = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  const original = submit.textContent;
  submit.disabled = true;
  submit.textContent = "문의 등록 중...";
  try {
    const result = await api("/api/tickets", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
    $("#support-complete-text").textContent = `문의번호 #${result.inquiry_id} · 관리자에게 알림을 전송했습니다.`;
    $("#support-complete").hidden = false;
    $("#support-complete").scrollIntoView({ behavior: "smooth", block: "center" });
    event.currentTarget.reset();
    event.currentTarget.elements.ticket_type.value = state.supportType;
    await loadInquiries();
    toast("문의를 등록했습니다.");
  } catch (error) {
    toast(error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = original;
  }
}

async function loadInquiries() {
  const target = $("#support-history");
  if (!target || !state.me) return;
  try {
    const data = await api("/api/inquiries");
    target.innerHTML = (data.inquiries || []).map(item => `<article class="order"><div class="order-head"><strong>문의 #${item.id}</strong><span class="badge">${item.status === "ANSWERED" ? "답변완료" : "확인중"}</span></div><p>${escapeHtml(item.content)}</p>${item.answer ? `<div class="inquiry-answer"><b>관리자 답변</b><p>${escapeHtml(item.answer)}</p></div>` : ""}<small>${new Date(item.created_at).toLocaleString("ko-KR")}</small></article>`).join("") || '<div class="empty">등록한 문의가 없습니다.</div>';
  } catch (error) { target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
}

async function loadAdminInquiries() {
  const target = $("#admin-inquiries");
  try {
    const data = await api("/api/admin/inquiries");
    state.adminInquiries = data.inquiries || [];
    target.innerHTML = state.adminInquiries.map(item => `<article class="admin-inquiry"><div><strong>#${item.id} · ${escapeHtml(item.nickname || item.username)}</strong><small>${escapeHtml(item.user_id)} · ${escapeHtml(item.inquiry_type)} · ${new Date(item.created_at).toLocaleString("ko-KR")}</small><p>${escapeHtml(item.content)}</p>${item.answer ? `<div class="inquiry-answer"><b>답변</b><p>${escapeHtml(item.answer)}</p></div>` : ""}</div><form data-inquiry-answer="${item.id}"><textarea name="answer" maxlength="3000" required placeholder="답변을 입력해 주세요.">${escapeHtml(item.answer || "")}</textarea><div class="admin-actions"><button class="button compact" type="submit">답변 보내기</button>${item.status === "ANSWERED" ? `<button class="button danger compact" type="button" data-delete-inquiry="${item.id}">문의 삭제</button>` : ""}</div></form></article>`).join("") || '<p class="admin-empty">등록된 문의가 없습니다.</p>';
    $$('[data-inquiry-answer]', target).forEach(form => { form.onsubmit = answerInquiry; });
    $$('[data-delete-inquiry]', target).forEach(button => { button.onclick = () => deleteInquiry(button.dataset.deleteInquiry); });
  } catch (error) { target.innerHTML = `<p class="admin-empty">${escapeHtml(error.message)}</p>`; }
}

async function answerInquiry(event) {
  event.preventDefault();
  const id = event.currentTarget.dataset.inquiryAnswer;
  try {
    await api(`/api/admin/inquiries/${id}/answer`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
    await loadAdminInquiries(); toast("문의 답변을 저장하고 Discord DM으로 전송했습니다.");
  } catch (error) { toast(error.message); }
}

async function deleteInquiry(inquiryId) {
  if (!confirm("이 문의를 삭제할까요?\n문의사항과 답변은 TXT 파일로 관리자 Discord DM에 먼저 전송됩니다.")) return;
  try {
    await api(`/api/admin/inquiries/${inquiryId}`, { method: "DELETE", body: "{}" });
    await loadAdminInquiries();
    toast("문의 기록을 DM으로 보내고 문의를 삭제했습니다.");
  } catch (error) { toast(error.message); }
}

async function openDirectDm(userId = null) {
  if (userId) state.directDmUserId = String(userId);
  await loadDirectDm(state.directDmUserId, false);
  if (state.directDmPoll) clearInterval(state.directDmPoll);
  state.directDmPoll = setInterval(() => loadDirectDm(state.directDmUserId, true), 3000);
}

async function loadDirectDm(userId = null, silent = false) {
  try {
    const data = await api(`/api/admin/direct-dm${userId ? `?user_id=${encodeURIComponent(userId)}` : ""}`);
    const sessions = $("#direct-dm-sessions");
    sessions.innerHTML = (data.sessions || []).map(session => `<button type="button" class="direct-dm-session ${String(session.user_id) === String(state.directDmUserId) ? "active" : ""}" data-direct-dm-user="${escapeHtml(session.user_id)}">${session.avatar_url ? `<img src="${escapeHtml(session.avatar_url)}" alt="">` : ""}<span><b>${escapeHtml(session.nickname || session.username)}</b><small>${escapeHtml(session.user_id)}</small></span></button>`).join("") || '<p class="admin-empty">아직 단독 DM 기록이 없습니다.</p>';
    $$('[data-direct-dm-user]', sessions).forEach(button => { button.onclick = () => openDirectDm(button.dataset.directDmUser); });
    const header = $("#direct-dm-header");
    const messageBox = $("#direct-dm-messages");
    const form = $("#direct-dm-send-form");
    if (!data.selected_user) {
      header.innerHTML = "<h2>유저를 선택해 주세요</h2><p>사용자 ID로 원하는 유저에게 봇 DM을 보낼 수 있습니다.</p>";
      messageBox.innerHTML = '<p class="admin-empty">왼쪽에서 대화를 선택하거나 사용자 ID를 입력해 주세요.</p>';
      form.elements.user_id.value = "";
      return;
    }
    state.directDmUserId = String(data.selected_user.user_id);
    form.elements.user_id.value = state.directDmUserId;
    header.innerHTML = `<div class="direct-dm-user">${data.selected_user.avatar_url ? `<img src="${escapeHtml(data.selected_user.avatar_url)}" alt="">` : ""}<span><h2>${escapeHtml(data.selected_user.nickname || data.selected_user.username)}</h2><p>${escapeHtml(state.directDmUserId)} · ${escapeHtml(data.selected_user.username || "")}</p></span></div>`;
    messageBox.innerHTML = (data.messages || []).map(message => `<div class="direct-dm-message ${message.sender_type === "ADMIN" ? "admin" : "user"}"><b>${escapeHtml(message.sender_name || (message.sender_type === "ADMIN" ? "관리자" : "유저"))}</b><p>${escapeHtml(message.content)}</p><small>${String(message.created_at || "").slice(0, 16).replace("T", " ")}</small></div>`).join("") || '<p class="admin-empty">아직 주고받은 메시지가 없습니다.</p>';
    messageBox.scrollTop = messageBox.scrollHeight;
  } catch (error) {
    if (!silent) toast(error.message);
  }
}

async function sendDirectDm(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  if (!data.user_id) return toast("DM을 보낼 유저를 먼저 선택해 주세요.");
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await api("/api/admin/direct-dm", { method: "POST", body: JSON.stringify(data) });
    form.elements.content.value = "";
    await loadDirectDm(data.user_id, false);
    toast("유저에게 DM을 보냈습니다.");
  } catch (error) { toast(error.message); }
  finally { submit.disabled = false; }
}

async function loadSubadmins() {
  try {
    const data = await api("/api/admin/subadmins");
    state.subadmins = data.subadmins || [];
    $("#subadmin-products").innerHTML = (data.products || []).map(product => `<label class="check"><input type="checkbox" name="product_ids" value="${product.id}"><span>${escapeHtml(product.name)}</span></label>`).join("") || '<p class="admin-empty">활성 상품이 없습니다.</p>';
    $("#subadmin-list").innerHTML = state.subadmins.map(item => `<div class="admin-row"><span><b>${escapeHtml(item.nickname || item.username)}</b><br><small>${escapeHtml(item.user_id)} · 담당 상품 ${item.product_ids.length}개</small></span><button class="remove" data-delete-subadmin="${item.user_id}">삭제</button></div>`).join("") || '<p class="admin-empty">등록된 부관리자가 없습니다.</p>';
    $$('[data-delete-subadmin]').forEach(button => { button.onclick = () => deleteSubadmin(button.dataset.deleteSubadmin); });
  } catch (error) { toast(error.message); }
}

async function saveSubadmin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  data.product_ids = [...form.querySelectorAll('[name="product_ids"]:checked')].map(input => Number(input.value));
  try { await api("/api/admin/subadmins", { method: "POST", body: JSON.stringify(data) }); form.reset(); await loadSubadmins(); toast("부관리자와 담당 상품을 저장했습니다."); }
  catch (error) { toast(error.message); }
}

async function deleteSubadmin(userId) {
  if (!confirm("이 부관리자를 삭제할까요?")) return;
  try { await api(`/api/admin/subadmins/${userId}`, { method: "DELETE", body: "{}" }); await loadSubadmins(); toast("부관리자를 삭제했습니다."); }
  catch (error) { toast(error.message); }
}

async function openSubadminPanel() {
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  if (!state.isSubadmin) {
    toast("등록된 부관리자만 접근할 수 있습니다.");
    goToPage("home");
    return;
  }
  await loadSubadminPanel();
}

async function loadSubadminPanel() {
  try {
    const data = await api("/api/subadmin/panel");
    state.adminCategories = data.categories || [];
    state.subadminProducts = data.products || [];
    state.subadminOrders = data.orders || [];
    renderSubadminPanel();
  } catch (error) {
    toast(error.message);
  }
}

function renderSubadminPanel() {
  const activeOrders = state.subadminOrders.filter(order => !["CANCELLED", "REJECTED"].includes(order.status));
  $("#subadmin-metrics").innerHTML = [
    ["담당 상품", `${state.subadminProducts.length}개`],
    ["전체 주문", `${state.subadminOrders.length}건`],
    ["진행 중", `${activeOrders.length}건`],
  ].map(([label, value]) => `<div class="metric"><small>${label}</small><strong>${value}</strong></div>`).join("");

  $("#subadmin-managed-products").innerHTML = state.subadminProducts.map(product => {
    const category = product.parent_category_name ? `${product.parent_category_name} › ${product.category_name}` : (product.category_name || "미분류");
    return `<div class="subadmin-product-row"><span><b>${escapeHtml(product.name)}</b> <span class="badge ${product.is_active ? "APPROVED" : "CANCELLED"}">${product.is_active ? "판매중" : "판매중지"}</span><small>${escapeHtml(category)} · ${money(product.price)}</small></span><span class="admin-actions"><button type="button" class="button secondary compact" data-subadmin-edit-product="${product.id}">상품 설정</button>${product.is_active ? `<button type="button" class="button danger compact" data-subadmin-delete-product="${product.id}">판매 중지</button>` : ""}</span></div>`;
  }).join("") || '<p class="admin-empty">배정된 상품이 없습니다. 관리자에게 담당 상품 지정을 요청해 주세요.</p>';

  $$('[data-subadmin-edit-product]').forEach(button => {
    button.onclick = () => {
      state.productEditorScope = "subadmin";
      openEditor("product", state.subadminProducts.find(product => product.id == button.dataset.subadminEditProduct));
    };
  });
  $$('[data-subadmin-delete-product]').forEach(button => {
    button.onclick = async () => {
      const product = state.subadminProducts.find(item => item.id == button.dataset.subadminDeleteProduct);
      if (!product || !confirm(`"${product.name}" 상품의 판매를 중지할까요?`)) return;
      try {
        await api(`/api/subadmin/products/${product.id}`, { method: "DELETE", body: "{}" });
        await Promise.all([loadSubadminPanel(), loadCatalog(), loadCart()]);
        toast("담당 상품의 판매를 중지했습니다.");
      } catch (error) { toast(error.message); }
    };
  });
  renderSubadminOrders();
}

function renderSubadminOrders() {
  const filters = [["all", "전체"], ["pending", "승인대기"], ["approved", "승인"], ["preparing", "상품준비중"], ["shipping-ready", "배송준비중"], ["shipping", "배송중"], ["delivered", "배송완료"], ["cancelled", "취소"]];
  const matches = (order, filter) => {
    const progress = order.fulfillment_status || "PAYMENT_APPROVED";
    if (filter === "all") return true;
    if (filter === "pending") return order.status === "PENDING";
    if (filter === "cancelled") return ["CANCELLED", "REJECTED"].includes(order.status);
    if (order.status !== "APPROVED") return false;
    return ({ approved: "PAYMENT_APPROVED", preparing: "PRODUCT_PREPARING", "shipping-ready": "SHIPPING_PREPARING", shipping: "SHIPPING", delivered: "DELIVERED" })[filter] === progress;
  };
  const filterBox = $("#subadmin-order-filters");
  filterBox.innerHTML = filters.map(([value, label]) => `<button type="button" class="${state.subadminOrderFilter === value ? "active" : ""}" data-subadmin-order-filter="${value}">${label}<b>${state.subadminOrders.filter(order => matches(order, value)).length}</b></button>`).join("");
  $$('[data-subadmin-order-filter]', filterBox).forEach(button => {
    button.onclick = () => { state.subadminOrderFilter = button.dataset.subadminOrderFilter; renderSubadminOrders(); };
  });
  const orders = state.subadminOrders.filter(order => matches(order, state.subadminOrderFilter));
  const target = $("#subadmin-managed-orders");
  target.innerHTML = orders.map(order => {
    const cancelled = ["CANCELLED", "REJECTED"].includes(order.status);
    const progress = order.fulfillment_status || "PAYMENT_APPROVED";
    const progressLabel = ({ PAYMENT_APPROVED: "승인", PRODUCT_PREPARING: "상품준비중", SHIPPING_PREPARING: "배송준비중", SHIPPING: "배송중", DELIVERED: "배송완료" })[progress] || progress;
    return `<article class="admin-order"><div class="admin-order-main"><div><strong>${escapeHtml(order.order_id)}</strong><span class="badge ${escapeHtml(order.status)}">${escapeHtml(cancelled || order.status === "PENDING" ? statusText(order.status) : progressLabel)}</span></div><p>${escapeHtml(order.product)} · ${escapeHtml(order.quantity)}</p><small>구매자 ${escapeHtml(order.buyer_name)} (${escapeHtml(order.buyer_id)}) · ${escapeHtml(order.amount)}</small><small>수령인 ${escapeHtml(order.shipping_name || "미등록")} · ${escapeHtml(order.shipping_contact || "미등록")}</small><small>배송지 ${escapeHtml(order.shipping_cvs || order.shipping_address || "미등록")}</small><small>배송메모 ${escapeHtml(order.shipping_memo || "없음")}</small>${order.cancel_reason ? `<small class="cancel-reason">취소 사유: ${escapeHtml(order.cancel_reason)}</small>` : ""}${order.waybill_number ? `<small>운송장: ${escapeHtml(order.waybill_number)}</small>` : ""}</div><div class="admin-order-actions">${cancelled ? `<button type="button" class="button danger compact" data-subadmin-delete-order="${escapeHtml(order.order_id)}">기록삭제</button>` : `${order.status === "APPROVED" ? `<button type="button" class="button secondary compact" data-subadmin-waybill="${escapeHtml(order.order_id)}">운송장 발송</button>` : ""}<button type="button" class="button danger compact" data-subadmin-cancel="${escapeHtml(order.order_id)}">주문취소</button>`}</div></article>`;
  }).join("") || '<p class="admin-empty">선택한 상태의 담당 주문이 없습니다.</p>';

  $$('[data-subadmin-cancel]', target).forEach(button => {
    button.onclick = async () => {
      const reason = prompt("구매자에게 전달할 주문취소 사유를 입력해 주세요.");
      if (reason === null) return;
      if (reason.trim().length < 2) return toast("주문취소 사유를 2자 이상 입력해 주세요.");
      if (!confirm("이 담당 상품 주문을 취소할까요?")) return;
      try { await api(`/api/subadmin/orders/${encodeURIComponent(button.dataset.subadminCancel)}/cancel`, { method: "POST", body: JSON.stringify({ reason }) }); await loadSubadminPanel(); toast("주문을 취소했습니다."); }
      catch (error) { toast(error.message); }
    };
  });
  $$('[data-subadmin-waybill]', target).forEach(button => {
    button.onclick = async () => {
      const waybill = prompt("발송할 운송장 번호를 입력해 주세요.");
      if (waybill === null || !waybill.trim()) return;
      try { await api(`/api/subadmin/orders/${encodeURIComponent(button.dataset.subadminWaybill)}/waybill`, { method: "POST", body: JSON.stringify({ waybill }) }); await loadSubadminPanel(); toast("운송장을 저장하고 구매자에게 DM을 보냈습니다."); }
      catch (error) { toast(error.message); }
    };
  });
  $$('[data-subadmin-delete-order]', target).forEach(button => {
    button.onclick = async () => {
      if (!confirm("취소된 주문 기록을 삭제할까요?")) return;
      try { await api(`/api/subadmin/orders/${encodeURIComponent(button.dataset.subadminDeleteOrder)}`, { method: "DELETE", body: "{}" }); await loadSubadminPanel(); toast("주문 기록을 삭제했습니다."); }
      catch (error) { toast(error.message); }
    };
  });
}

function setAdminSection(section) {
  $$('[data-admin-section]').forEach(panel => panel.classList.toggle("active", panel.dataset.adminSection === section));
  $$('[data-admin-section-target]').forEach(button => button.classList.toggle("active", button.dataset.adminSectionTarget === section));
  if (section === "blacklist") loadBlacklist();
  if (section === "users") loadAdminUsers();
  if (section === "policies") loadAdminPolicies();
  if (section === "coupons") loadAdminCouponEvents();
  if (section === "inquiries") loadAdminInquiries();
  if (section === "direct-dm") openDirectDm(state.directDmUserId);
  else if (state.directDmPoll) { clearInterval(state.directDmPoll); state.directDmPoll = null; }
  if (section === "subadmins") loadSubadmins();
}

function updateBlacklistDuration() {
  const temporary = $("#blacklist-type").value === "TEMPORARY";
  $("#blacklist-duration-fields").hidden = !temporary;
  $("#blacklist-custom-date").hidden = !temporary || $("#blacklist-duration").value !== "custom";
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
      ? `<small>입금해야 할 금액</small><b class="payment-amount">${money(data.cash_amount)}</b><strong>${escapeHtml(data.bank.bank_name || "은행 미설정")} ${escapeHtml(data.bank.account_number || "")}</strong><span>예금주 ${escapeHtml(data.bank.account_holder || "미설정")}</span>`
      : "포인트 전액결제로 승인되었습니다.";
    goToPage("complete");
    startCountdown(data);
  } catch (error) {
    toast(error.message);
    if (error.message.includes("정보등록")) goToPage("account");
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

async function openAdminPanel() {
  if (!state.me) {
    location.href = "/auth/login";
    return;
  }
  if (!state.isAdmin) {
    toast("관리자 계정만 접근할 수 있습니다.");
    goToPage("home");
    return;
  }
  setAdminSection("dashboard");
  await loadAdmin();
  const requested = new URLSearchParams(location.search).get("admin_section");
  if (requested) setAdminSection(requested);
}

async function loadAdmin(period = "week") {
  if (!state.me) return;
  const results = await Promise.allSettled([
    api(`/api/admin/dashboard?period=${period}`),
    api("/api/admin/categories"),
    api("/api/admin/products"),
    api("/api/admin/channels"),
    api("/api/admin/orders"),
    api("/api/admin/notices"),
  ]);
  const failures = results.filter(result => result.status === "rejected");
  const [dashboardResult, categoriesResult, productsResult, channelsResult, ordersResult, noticesResult] = results;
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
  if (ordersResult.status === "fulfilled") {
    state.adminOrders = ordersResult.value.orders || [];
    renderAdminOrders();
  }
  if (noticesResult.status === "fulfilled") {
    $$(".notice-admin-form").forEach(form => {
      const item = (noticesResult.value.notices || []).find(row => row.notice_type === form.dataset.noticeType);
      form.elements.content.value = item?.content || "";
      form.elements.is_active.checked = Boolean(item?.is_active);
      form.elements.image.value = "";
      form.elements.remove_image.checked = false;
      const preview = form.querySelector(".notice-image-preview");
      preview.dataset.existingSrc = item?.image_url || "";
      if (item?.image_url) { preview.src = item.image_url; preview.hidden = false; }
      else { preview.removeAttribute("src"); preview.hidden = true; }
    });
  }
  if (failures.length) toast(failures[0].reason?.message || "관리자 정보를 일부 불러오지 못했습니다.");
}

async function loadBlacklist() {
  const button = $("#refresh-blacklist");
  if (button) button.disabled = true;
  try {
    const data = await api("/api/admin/blacklist");
    state.blacklist = data.blacklist || [];
    renderBlacklist();
  } catch (error) {
    toast(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

function renderBlacklist() {
  const target = $("#blacklist-list");
  if (!target) return;
  if (!state.blacklist.length) {
    target.innerHTML = '<p class="admin-empty">등록된 블랙리스트 사용자가 없습니다.</p>';
    return;
  }
  target.innerHTML = state.blacklist.map(entry => {
    const created = new Date(entry.created_at);
    const createdText = Number.isNaN(created.getTime()) ? "" : created.toLocaleString("ko-KR");
    const avatar = entry.avatar_url
      ? `<img class="blacklist-avatar" src="${escapeHtml(entry.avatar_url)}" alt="${escapeHtml(entry.nickname)} 프로필 사진" loading="lazy">`
      : `<span class="blacklist-avatar fallback" aria-hidden="true">?</span>`;
    return `
      <article class="blacklist-row">
        ${avatar}
        <div class="blacklist-user">
          <div><strong>${escapeHtml(entry.nickname)}</strong><span>@${escapeHtml(entry.username)}</span></div>
          <code>사용자 ID · ${escapeHtml(entry.user_id)}</code>
          <p><b>${entry.blacklist_type === "TEMPORARY" ? "임시" : "영구"}</b>${escapeHtml(entry.reason)}${entry.expires_at ? ` · ${escapeHtml(new Date(entry.expires_at).toLocaleString("ko-KR"))}까지` : ""}</p>
        </div>
        <div class="blacklist-meta">
          ${createdText ? `<time>${escapeHtml(createdText)}</time>` : ""}
          <button type="button" class="button danger compact" data-blacklist-remove="${escapeHtml(entry.user_id)}">해제</button>
        </div>
      </article>`;
  }).join("");
  $$('[data-blacklist-remove]', target).forEach(button => {
    button.onclick = async () => {
      if (!confirm("이 사용자를 블랙리스트에서 해제할까요?")) return;
      button.disabled = true;
      try {
        await api(`/api/admin/blacklist/${encodeURIComponent(button.dataset.blacklistRemove)}`, { method: "DELETE", body: "{}" });
        await loadBlacklist();
        toast("블랙리스트에서 해제했습니다.");
      } catch (error) {
        button.disabled = false;
        toast(error.message);
      }
    };
  });
}

async function loadAdminUsers() {
  try {
    const data = await api("/api/admin/users");
    state.adminUsers = data.users || [];
    renderAdminUsers();
  } catch (error) { toast(error.message); }
}

function renderAdminUsers() {
  const target = $("#admin-users");
  if (!target) return;
  target.innerHTML = state.adminUsers.length ? state.adminUsers.map(user => `
    <details class="admin-user-row">
      <summary>${user.avatar_url ? `<img src="${escapeHtml(user.avatar_url)}" alt="">` : '<span class="blacklist-avatar fallback">?</span>'}<span><strong>${escapeHtml(user.nickname)}</strong><small>@${escapeHtml(user.username)} · ${escapeHtml(user.user_id)}</small></span><b>${Number(user.points || 0).toLocaleString()}P</b></summary>
      <div class="admin-user-details"><p><b>실명</b>${escapeHtml(user.name || "미등록")}</p><p><b>전화번호</b>${escapeHtml(user.contact || "미등록")}</p><p><b>집주소</b>${escapeHtml(user.address || "미등록")}</p><p><b>편의점</b>${escapeHtml([user.cvs_brand, user.cvs, user.cvs_address].filter(Boolean).join(" · ") || "미등록")}</p><p><b>환불계좌</b>${escapeHtml([user.refund_bank, user.refund_account_number, user.refund_account_holder].filter(Boolean).join(" · ") || "미등록")}</p><p><b>누적 구매</b>${money(user.total_spent)}</p></div>
    </details>`).join("") : '<p class="admin-empty">등록된 유저 정보가 없습니다.</p>';
}

async function saveAdminPoints(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.all_users = Boolean(data.all_users);
  if (data.all_users && !confirm("등록된 모든 유저에게 이 포인트를 적용할까요?")) return;
  try {
    const result = await api("/api/admin/points", { method: "POST", body: JSON.stringify(data) });
    await loadAdminUsers();
    toast(data.all_users ? `${result.count}명에게 포인트를 적용했습니다.` : `포인트를 적용했습니다. 현재 ${Number(result.balance).toLocaleString()}P`);
  } catch (error) { toast(error.message); }
}

async function saveNotice(event) {
  event.preventDefault();
  const form = event.target;
  const file = form.elements.image.files?.[0];
  if (file && file.size > 5 * 1024 * 1024) return toast("공지 이미지는 5MB 이하로 업로드해 주세요.");
  const data = Object.fromEntries(new FormData(form));
  delete data.image;
  data.notice_type = form.dataset.noticeType;
  data.is_active = form.elements.is_active.checked;
  data.remove_image = form.elements.remove_image.checked;
  if (file) {
    data.image_data_url = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }
  try {
    await api("/api/admin/notices", { method: "PUT", body: JSON.stringify(data) });
    form.elements.image.value = "";
    form.elements.remove_image.checked = false;
    const preview = form.querySelector(".notice-image-preview");
    if (data.remove_image) {
      preview.dataset.existingSrc = "";
      preview.removeAttribute("src");
      preview.hidden = true;
    } else if (data.image_data_url) {
      preview.dataset.existingSrc = data.image_data_url;
      preview.src = data.image_data_url;
      preview.hidden = false;
    }
    await refreshNoticeIndicators();
    toast(`${data.notice_type === "NOTICE" ? "공지" : "이벤트"}를 저장했습니다.`);
  } catch (error) { toast(error.message); }
}

function previewNoticeImage(event) {
  const input = event.target;
  const form = input.closest("form");
  const preview = form.querySelector(".notice-image-preview");
  const file = input.files?.[0];
  if (!file) {
    if (preview.dataset.existingSrc) { preview.src = preview.dataset.existingSrc; preview.hidden = false; }
    else preview.hidden = true;
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    toast("공지 이미지는 5MB 이하로 업로드해 주세요.");
    input.value = "";
    return;
  }
  form.elements.remove_image.checked = false;
  const reader = new FileReader();
  reader.onload = () => { preview.src = reader.result; preview.hidden = false; };
  reader.readAsDataURL(file);
}

async function issueCoupon(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.all_users = Boolean(data.all_users);
  if (data.all_users && !confirm("등록된 모든 유저에게 쿠폰을 한 장씩 지급할까요?")) return;
  try {
    const result = await api("/api/admin/coupons", { method: "POST", body: JSON.stringify(data) });
    toast(data.all_users ? `${result.count}명에게 쿠폰을 지급했습니다.` : `쿠폰을 지급했습니다. 코드: ${result.code}`);
    event.target.reset(); event.target.elements.days.value = 30;
    updateCouponRecipient();
  } catch (error) { toast(error.message); }
}

function previewCouponEventImage(event) {
  const file = event.target.files?.[0];
  const preview = $("#coupon-event-image-preview");
  if (!file) { preview.hidden = true; preview.removeAttribute("src"); return; }
  if (file.size > 5 * 1024 * 1024) {
    toast("쿠폰 이미지는 5MB 이하로 업로드해 주세요.");
    event.target.value = ""; preview.hidden = true; return;
  }
  const reader = new FileReader();
  reader.onload = () => { preview.src = reader.result; preview.hidden = false; };
  reader.readAsDataURL(file);
}

async function createCouponEvent(event) {
  event.preventDefault();
  const file = event.target.elements.image.files?.[0];
  if (!file) return toast("쿠폰 이미지를 업로드해 주세요.");
  const submit = event.submitter || event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const imageDataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file);
    });
    const data = Object.fromEntries(new FormData(event.target));
    delete data.image;
    data.image_data_url = imageDataUrl;
    await api("/api/admin/coupon-events", { method: "POST", body: JSON.stringify(data) });
    event.target.reset();
    $("#coupon-event-image-preview").hidden = true;
    await loadAdminCouponEvents();
    await loadCouponEvents(false);
    toast("이벤트 페이지에 쿠폰을 등록했습니다.");
  } catch (error) { toast(error.message); }
  finally { submit.disabled = false; }
}

async function loadAdminCouponEvents() {
  const target = $("#admin-coupon-events");
  try {
    const data = await api("/api/admin/coupon-events");
    state.adminCouponEvents = data.events || [];
    target.innerHTML = state.adminCouponEvents.length ? state.adminCouponEvents.map(event => `
      <article class="admin-coupon-event-row">
        <div><strong>${escapeHtml(event.title)}</strong><p>${escapeHtml(event.description || "설명 없음")}</p><small>${money(event.discount_amount)} 할인 · 수령 후 ${Number(event.valid_days)}일 · 받은 인원 ${Number(event.claim_count)}명${Number(event.max_claims || 0) ? `/${Number(event.max_claims)}명` : " · 인원 제한 없음"}</small><small>${escapeHtml(couponEventConditions(event).join(" · "))}</small></div>
        <button type="button" class="button danger compact" data-delete-coupon-event="${escapeHtml(event.event_key)}" data-coupon-title="${escapeHtml(event.title)}">이벤트창에서 삭제</button>
      </article>`).join("") : '<p class="admin-empty">현재 등록된 이벤트 쿠폰이 없습니다.</p>';
    $$('[data-delete-coupon-event]', target).forEach(button => {
      button.onclick = () => deleteAdminCouponEvent(button.dataset.deleteCouponEvent, button.dataset.couponTitle);
    });
  } catch (error) {
    target.innerHTML = `<p class="admin-empty">${escapeHtml(error.message)}</p>`;
  }
}

function couponEventConditions(event) {
  const purchaserLabels = {
    ALL: "구매 이력 제한 없음",
    FIRST_PURCHASE: "첫 구매 전용",
    EXISTING_BUYER: "기존 구매자 전용",
  };
  return [
    event.minimum_tier === "NONE" ? "등급 제한 없음" : `${event.minimum_tier_label || String(event.minimum_tier).replaceAll("_", " ")} 이상`,
    purchaserLabels[event.purchaser_requirement] || "구매 이력 제한 없음",
    Number(event.min_order_amount || 0) ? `${money(event.min_order_amount)} 이상 구매 시 사용` : "최소 주문금액 없음",
  ];
}

async function deleteAdminCouponEvent(eventKey, title) {
  if (!confirm(`'${title}' 쿠폰을 이벤트창에서 삭제할까요?\n삭제 후에는 새로 받을 수 없으며, 이미 받은 쿠폰은 유지됩니다.`)) return;
  try {
    await api(`/api/admin/coupon-events/${encodeURIComponent(eventKey)}`, { method: "DELETE", body: "{}" });
    await Promise.all([loadAdminCouponEvents(), loadCouponEvents(false)]);
    toast("이벤트 쿠폰을 삭제했습니다. 이제 새로 받을 수 없습니다.");
  } catch (error) {
    toast(error.message);
  }
}

async function loadCouponEvents(showWelcome = false) {
  if (!state.me) return;
  const [couponData, noticeData] = await Promise.all([
    api("/api/coupon-events"),
    api("/api/notices"),
  ]);
  state.couponEvents = couponData.events || [];
  state.eventNotices = (noticeData.notices || []).filter(item => item.notice_type === "EVENT");
  state.welcomePending = Boolean(couponData.welcome_pending);
  renderCouponEvents();
  updateNoticeIndicators(noticeData.notices || [], state.couponEvents);
  if (showWelcome && state.welcomePending) {
    $("#welcome-coupon-modal").hidden = false;
    document.body.classList.add("modal-open");
  }
  if (!showWelcome && currentPageId() === "events") {
    const latest = Math.max(
      latestUpdate(state.eventNotices, "updated_at"),
      latestUpdate(state.couponEvents.filter(item => item.event_key !== "WELCOME"), "created_at"),
    );
    if (latest) localStorage.setItem(EVENT_SEEN_KEY, String(latest));
    updateNoticeIndicators();
  }
}

function renderCouponEvents() {
  const announcements = $("#event-announcement-list");
  if (announcements) {
    announcements.hidden = !state.eventNotices.length;
    announcements.innerHTML = state.eventNotices.map(item => `
      <article class="event-announcement-card">
        ${item.image_url ? `<img class="announcement-image" src="${escapeHtml(item.image_url)}" alt="이벤트 이미지">` : '<div class="event-announcement-mark" aria-hidden="true">EVENT</div>'}
        <div><p class="eyebrow">NOW ON V4P3 SP0T</p><h2>진행 중인 이벤트</h2><p>${escapeHtml(item.content)}</p></div>
      </article>`).join("");
  }
  const target = $("#coupon-event-list");
  if (!target) return;
  target.innerHTML = state.couponEvents.length ? state.couponEvents.map(event => `
    <article class="coupon-event-card">
      <img src="${escapeHtml(event.image_url)}" alt="${escapeHtml(event.title)} 쿠폰 이미지" loading="lazy">
      <div><p class="eyebrow">COUPON EVENT</p><h2>${escapeHtml(event.title)}</h2><p>${escapeHtml(event.description || "이벤트 쿠폰을 받아 결제할 때 사용해 주세요.")}</p><strong>${money(event.discount_amount)} 할인</strong><small>수령일부터 ${Number(event.valid_days)}일 동안 사용 가능</small><div class="coupon-condition-list">${couponEventConditions(event).map(condition => `<span>${escapeHtml(condition)}</span>`).join("")}${Number(event.max_claims || 0) ? `<span>선착순 ${Number(event.max_claims)}명 · 현재 ${Number(event.claim_count)}명 수령</span>` : ""}</div>${event.eligibility_reason ? `<small class="coupon-ineligible">${escapeHtml(event.eligibility_reason)}</small>` : ""}</div>
      <button type="button" class="button ${event.claimed || !event.eligible ? "secondary" : ""}" data-claim-coupon="${escapeHtml(event.event_key)}" ${event.claimed || !event.eligible ? "disabled" : ""}>${event.claimed ? "받은 쿠폰" : event.eligible ? "쿠폰 받기" : "조건 미충족"}</button>
    </article>`).join("") : '<div class="empty">현재 받을 수 있는 이벤트 쿠폰이 없습니다.</div>';
  $$('[data-claim-coupon]', target).forEach(button => {
    button.onclick = () => claimCouponEvent(button.dataset.claimCoupon, false);
  });
}

function latestUpdate(items, field) {
  return items.reduce((latest, item) => Math.max(latest, Date.parse(item[field] || "") || 0), 0);
}

function updateNoticeIndicators(notices = state.notices, couponEvents = state.couponEvents) {
  state.notices = notices;
  const noticeUpdates = notices.filter(item => item.notice_type === "NOTICE");
  const eventUpdates = notices.filter(item => item.notice_type === "EVENT");
  const noticeLatest = latestUpdate(noticeUpdates, "updated_at");
  const eventLatest = Math.max(
    latestUpdate(eventUpdates, "updated_at"),
    latestUpdate(couponEvents.filter(item => item.event_key !== "WELCOME"), "created_at"),
  );
  $("#notices-nav-dot").hidden = !noticeLatest || noticeLatest <= Number(localStorage.getItem(NOTICE_SEEN_KEY) || 0);
  $("#events-nav-dot").hidden = !eventLatest || eventLatest <= Number(localStorage.getItem(EVENT_SEEN_KEY) || 0);
}

async function refreshNoticeIndicators() {
  const data = await api("/api/notices");
  updateNoticeIndicators(data.notices || [], state.couponEvents);
}

async function loadNoticesPage() {
  const target = $("#notice-page-list");
  try {
    const data = await api("/api/notices");
    state.notices = data.notices || [];
    const notices = state.notices.filter(item => item.notice_type === "NOTICE");
    target.innerHTML = notices.length ? notices.map(item => `
      <article class="notice-page-card">
        ${item.image_url ? `<img class="announcement-image" src="${escapeHtml(item.image_url)}" alt="공지 이미지">` : '<div class="notice-page-mark" aria-hidden="true">NOTICE</div>'}
        <div><p class="eyebrow">OFFICIAL NOTICE</p><h2>공지사항</h2><p>${escapeHtml(item.content)}</p><time>${new Date(item.updated_at).toLocaleString("ko-KR")}</time></div>
      </article>`).join("") : '<div class="empty">현재 등록된 공지사항이 없습니다.</div>';
    const latest = latestUpdate(notices, "updated_at");
    if (latest) localStorage.setItem(NOTICE_SEEN_KEY, String(latest));
    updateNoticeIndicators();
  } catch (error) {
    target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

async function claimCouponEvent(eventKey, welcome) {
  const button = welcome ? $("#claim-welcome-coupon") : $(`[data-claim-coupon="${eventKey}"]`);
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/coupon-events/${encodeURIComponent(eventKey)}/claim`, { method: "POST", body: "{}" });
    toast(`${money(result.discount_amount)} 할인쿠폰을 받았습니다.`);
    if (welcome) {
      $("#welcome-coupon-modal").hidden = true;
      document.body.classList.remove("modal-open");
      state.welcomePending = false;
      loadSiteMessages().catch(() => showDiscordNotice());
    }
    await loadCouponEvents(false);
  } catch (error) {
    toast(error.message);
    if (button) button.disabled = false;
  }
}

async function checkDeliveredReviewPrompt() {
  if (!state.me || state.welcomePending) return false;
  const data = await api("/api/orders");
  const order = (data.orders || []).find(item => (
    item.status === "APPROVED" && item.fulfillment_status === "DELIVERED" && !item.reviewed
  ));
  if (!order || sessionStorage.getItem(`v4p3ReviewLater:${order.order_id}`) === "1") return false;
  const modal = $("#delivery-review-modal");
  modal.dataset.orderId = order.order_id;
  modal.dataset.product = order.product || "";
  $("#delivery-review-title").textContent = `${state.me.username}님! 배송받으신 상품이 어떠신가요?`;
  $("#delivery-review-product").textContent = order.product || "배송완료 상품";
  modal.hidden = false;
  document.body.classList.add("modal-open");
  return true;
}

function closeDeliveredReviewPrompt() {
  const modal = $("#delivery-review-modal");
  if (modal.dataset.orderId) sessionStorage.setItem(`v4p3ReviewLater:${modal.dataset.orderId}`, "1");
  modal.hidden = true;
  document.body.classList.remove("modal-open");
}

function beginOrderReview(orderId, product) {
  state.reviewOrderId = String(orderId || "");
  state.reviewOrderProduct = String(product || "");
  const form = $("#review-form");
  if (form) {
    form.elements.order_id.value = state.reviewOrderId;
    $("#review-order-product").textContent = state.reviewOrderProduct;
    $("#review-order-context").hidden = !state.reviewOrderId;
  }
  try {
    sessionStorage.setItem("v4p3PendingReview", JSON.stringify({ orderId: state.reviewOrderId, product: state.reviewOrderProduct }));
  } catch {}
  goToPage("review-write");
}

function restoreOrderReviewContext() {
  if (!$("#review-form")) return;
  try {
    const pending = JSON.parse(sessionStorage.getItem("v4p3PendingReview") || "null");
    if (pending?.orderId) {
      state.reviewOrderId = String(pending.orderId);
      state.reviewOrderProduct = String(pending.product || "");
      $("#review-form").elements.order_id.value = state.reviewOrderId;
      $("#review-order-product").textContent = state.reviewOrderProduct;
      $("#review-order-context").hidden = false;
    }
  } catch {}
}

function clearOrderReviewContext() {
  state.reviewOrderId = null;
  state.reviewOrderProduct = "";
  const form = $("#review-form");
  form.elements.order_id.value = "";
  $("#review-order-context").hidden = true;
  try { sessionStorage.removeItem("v4p3PendingReview"); } catch {}
}

async function loadSiteMessages() {
  const data = await api("/api/notices");
  state.siteMessages = (data.notices || []).sort((a, b) => (a.notice_type === "NOTICE" ? -1 : 1)).filter(item => Number(localStorage.getItem(`siteMessage:${item.notice_type}:${item.updated_at}`) || 0) < Date.now());
  showNextSiteMessage();
}

function showNextSiteMessage() {
  const item = state.siteMessages.shift();
  const modal = $("#site-message");
  if (!item) {
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    checkDeliveredReviewPrompt().then(shown => { if (!shown) showDiscordNotice(); }).catch(() => showDiscordNotice());
    return;
  }
  modal.dataset.key = `siteMessage:${item.notice_type}:${item.updated_at}`;
  $("#site-message-type").textContent = item.notice_type;
  $("#site-message-title").textContent = item.notice_type === "NOTICE" ? "공지사항" : "진행 중인 이벤트";
  $("#site-message-content").textContent = item.content;
  const image = $("#site-message-image");
  image.hidden = !item.image_url;
  if (item.image_url) { image.src = item.image_url; image.alt = item.notice_type === "NOTICE" ? "공지 이미지" : "이벤트 이미지"; }
  else image.removeAttribute("src");
  modal.hidden = false; document.body.classList.add("modal-open");
}

function dismissSiteMessageDay() {
  const key = $("#site-message").dataset.key;
  if (key) localStorage.setItem(key, String(Date.now() + 86400000));
  showNextSiteMessage();
}

async function addBlacklist(event) {
  event.preventDefault();
  const submit = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    await api("/api/admin/blacklist", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.target))) });
    event.target.reset();
    updateBlacklistDuration();
    await loadBlacklist();
    toast("블랙리스트에 등록했습니다.");
  } catch (error) {
    toast(error.message);
  } finally {
    submit.disabled = false;
  }
}

async function removeBlacklist(event) {
  event.preventDefault();
  const userId = String(new FormData(event.target).get("user_id") || "").trim();
  if (!confirm("입력한 사용자를 블랙리스트에서 해제할까요?")) return;
  const submit = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    await api(`/api/admin/blacklist/${encodeURIComponent(userId)}`, { method: "DELETE", body: "{}" });
    event.target.reset();
    await loadBlacklist();
    toast("블랙리스트에서 해제했습니다.");
  } catch (error) {
    toast(error.message);
  } finally {
    submit.disabled = false;
  }
}

async function loadAdminOrders() {
  try {
    const data = await api("/api/admin/orders");
    state.adminOrders = data.orders || [];
    renderAdminOrders();
  } catch (error) {
    toast(error.message);
  }
}

function renderAdminOrders() {
  const target = $("#admin-orders");
  if (!target) return;
  const filters = [
    ["all", "전체"], ["pending", "입금대기"], ["approved", "승인"],
    ["preparing", "상품준비중"], ["shipping-ready", "배송준비중"],
    ["shipping", "배송중"], ["delivered", "배송완료"], ["cancelled", "취소"],
  ];
  const matches = (order, filter) => {
    const progress = order.fulfillment_status || "PAYMENT_APPROVED";
    if (filter === "all") return true;
    if (filter === "pending") return order.status === "PENDING";
    if (filter === "cancelled") return ["CANCELLED", "REJECTED"].includes(order.status);
    if (order.status !== "APPROVED") return false;
    return ({ approved: "PAYMENT_APPROVED", preparing: "PRODUCT_PREPARING", "shipping-ready": "SHIPPING_PREPARING", shipping: "SHIPPING", delivered: "DELIVERED" })[filter] === progress;
  };
  const filterBox = $("#admin-order-filters");
  filterBox.innerHTML = filters.map(([value, label]) => {
    const count = state.adminOrders.filter(order => matches(order, value)).length;
    return `<button type="button" class="${state.adminOrderFilter === value ? "active" : ""}" data-order-filter="${value}">${label}<b>${count}</b></button>`;
  }).join("");
  $$('[data-order-filter]', filterBox).forEach(button => {
    button.onclick = () => { state.adminOrderFilter = button.dataset.orderFilter; renderAdminOrders(); };
  });
  const orders = state.adminOrders.filter(order => matches(order, state.adminOrderFilter));
  target.innerHTML = orders.length ? orders.map(order => {
    const cancelled = ["CANCELLED", "REJECTED"].includes(order.status);
    const progress = order.fulfillment_status || "PAYMENT_APPROVED";
    const progressLabel = ({ PAYMENT_APPROVED: "승인", PRODUCT_PREPARING: "상품준비중", SHIPPING_PREPARING: "배송준비중", SHIPPING: "배송중", DELIVERED: "배송완료" })[progress] || progress;
    return `
    <article class="admin-order">
      <div class="admin-order-main">
        <div><strong>${escapeHtml(order.order_id)}</strong><span class="badge ${escapeHtml(order.status)}">${escapeHtml(cancelled || order.status === "PENDING" ? statusText(order.status) : progressLabel)}</span></div>
        <p>${escapeHtml(order.product)} · ${escapeHtml(order.quantity)}</p>
        <small>${escapeHtml(order.buyer_name)} · ${escapeHtml(order.amount)} · ${String(order.created_at).slice(0, 16).replace("T", " ")}</small>
        <small>수령인 ${escapeHtml(order.shipping_name || "미등록")} · ${escapeHtml(order.shipping_contact || "미등록")} · ${escapeHtml(order.shipping_method || "배송방식 미등록")}</small>
        <small>배송지 ${escapeHtml(order.shipping_cvs || order.shipping_address || "미등록")}</small>
        <small>배송메모 ${escapeHtml(order.shipping_memo || "없음")}</small>
        <small>현금 ${money(order.cash_amount)} · 포인트 ${Number(order.points_used || 0).toLocaleString()}P</small>
        ${order.cancel_reason ? `<small class="cancel-reason">취소 사유: ${escapeHtml(order.cancel_reason)}</small>` : ""}
        ${order.waybill_number ? `<small>운송장: ${escapeHtml(order.waybill_number)}</small>` : ""}
      </div>
      <div class="admin-order-actions">
        ${cancelled
          ? `<button type="button" class="button danger compact" data-admin-delete-order="${escapeHtml(order.order_id)}">기록삭제</button>`
          : `${order.status === "APPROVED" ? `<button type="button" class="button compact" data-admin-prepare="${escapeHtml(order.order_id)}" ${progress === "PRODUCT_PREPARING" ? "disabled" : ""}>상품준비</button><label class="admin-order-status"><span>주문 상태</span><select data-admin-order-status="${escapeHtml(order.order_id)}"><option value="PAYMENT_APPROVED" ${progress === "PAYMENT_APPROVED" ? "selected" : ""}>승인</option><option value="PRODUCT_PREPARING" ${progress === "PRODUCT_PREPARING" ? "selected" : ""}>상품준비중</option><option value="SHIPPING_PREPARING" ${progress === "SHIPPING_PREPARING" ? "selected" : ""}>배송준비중</option><option value="SHIPPING" ${progress === "SHIPPING" ? "selected" : ""}>배송중</option><option value="DELIVERED" ${progress === "DELIVERED" ? "selected" : ""}>배송완료</option></select></label><button type="button" class="button secondary compact" data-admin-waybill="${escapeHtml(order.order_id)}">운송장 발송</button>` : ""}<button type="button" class="button danger compact" data-admin-cancel="${escapeHtml(order.order_id)}">주문취소</button>`}
      </div>
    </article>
  `; }).join("") : '<p class="admin-empty">선택한 상태의 주문이 없습니다.</p>';
  $$('[data-admin-prepare]', target).forEach(button => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        await updateAdminOrderStatus(button.dataset.adminPrepare, "PRODUCT_PREPARING");
        toast("주문을 상품준비중으로 변경했습니다.");
      } catch (error) {
        button.disabled = false;
        toast(error.message);
      }
    };
  });
  $$('[data-admin-order-status]', target).forEach(select => {
    select.onchange = async () => {
      const previous = state.adminOrders.find(order => String(order.order_id) === String(select.dataset.adminOrderStatus))?.fulfillment_status || "PAYMENT_APPROVED";
      select.disabled = true;
      try {
        await updateAdminOrderStatus(select.dataset.adminOrderStatus, select.value);
        toast(`주문 상태를 ${select.options[select.selectedIndex].text}으로 변경했습니다.`);
      } catch (error) {
        select.value = previous;
        select.disabled = false;
        toast(error.message);
      }
    };
  });
  $$('[data-admin-cancel]', target).forEach(button => {
    button.onclick = async () => {
      const reason = prompt("구매자에게 전달할 주문취소 사유를 입력해 주세요.");
      if (reason === null) return;
      if (reason.trim().length < 2) return toast("주문취소 사유를 2자 이상 입력해 주세요.");
      if (!confirm("이 주문을 취소하고 구매자에게 환불 안내를 보낼까요?")) return;
      button.disabled = true;
      try {
        await api(`/api/admin/orders/${encodeURIComponent(button.dataset.adminCancel)}/cancel`, { method: "POST", body: JSON.stringify({ reason }) });
        await loadAdminOrders();
        toast("주문을 취소하고 구매자에게 안내했습니다.");
      } catch (error) {
        button.disabled = false;
        toast(error.message);
      }
    };
  });
  $$('[data-admin-waybill]', target).forEach(button => {
    button.onclick = async () => {
      const waybill = prompt("발송할 운송장 번호를 입력해 주세요.");
      if (waybill === null) return;
      if (!waybill.trim()) return toast("운송장 번호를 입력해 주세요.");
      button.disabled = true;
      try {
        await api(`/api/admin/orders/${encodeURIComponent(button.dataset.adminWaybill)}/waybill`, { method: "POST", body: JSON.stringify({ waybill }) });
        await loadAdminOrders();
        toast("운송장을 저장하고 구매자에게 DM을 보냈습니다.");
      } catch (error) {
        button.disabled = false;
        toast(error.message);
      }
    };
  });
  $$('[data-admin-delete-order]', target).forEach(button => {
    button.onclick = async () => {
      if (!confirm("취소된 주문 기록을 완전히 삭제할까요? 삭제 후 복구할 수 없습니다.")) return;
      button.disabled = true;
      try {
        await api(`/api/admin/orders/${encodeURIComponent(button.dataset.adminDeleteOrder)}`, { method: "DELETE", body: "{}" });
        await loadAdminOrders();
        toast("취소된 주문 기록을 삭제했습니다.");
      } catch (error) {
        button.disabled = false;
        toast(error.message);
      }
    };
  });
}

async function updateAdminOrderStatus(orderId, fulfillmentStatus) {
  await api(`/api/admin/orders/${encodeURIComponent(orderId)}/status`, {
    method: "POST",
    body: JSON.stringify({ fulfillment_status: fulfillmentStatus }),
  });
  await loadAdminOrders();
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
    const optionGroups = productOptionGroups(product);
    const categoryPath = product.parent_category_name ? `${product.parent_category_name} › ${product.category_name}` : (product.category_name || "미분류");
    const optionSummary = optionGroups.map(group => group.type === "text" ? `${escapeHtml(group.label)}: 구매자 직접입력` : `${escapeHtml(group.label)}: ${group.options.map(escapeHtml).join(", ")}`).join("<br>");
    return `<div class="admin-row"><span class="admin-product-info"><input class="admin-product-check" type="checkbox" value="${product.id}" aria-label="${escapeHtml(product.name)} 선택"><span><b>${escapeHtml(product.name)}</b><br>${escapeHtml(categoryPath)} · ${money(product.price)}${optionSummary ? `<br><small>${optionSummary}</small>` : ""}</span></span><span class="admin-actions"><button class="text-button" data-edit-product="${product.id}">수정</button><button class="remove" data-del-product="${product.id}">삭제</button></span></div>`;
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
  $$("[data-edit-product]").forEach(button => { button.onclick = () => { state.productEditorScope = "admin"; openEditor("product", state.adminProducts.find(item => item.id == button.dataset.editProduct)); }; });
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
      <label>단독 구매 무료배송 수량
        <input name="free_shipping_quantity" type="number" inputmode="numeric" min="0" max="99" value="0">
        <small>0은 사용 안 함 · 이 상품만 설정 수량 이상 결제할 때 배송비가 0원입니다.</small>
      </label>
      <h3>선택 항목 <small>최대 3개</small></h3>
      ${optionGroupEditors({})}
      <small>선택 항목은 반드시 1번부터 순서대로 만드세요. 구매자 직접입력 질문을 선택하면 작성한 질문에 구매자가 장바구니에 담기 전에 답변합니다. 선택형 항목값에 (+2000)을 붙이면 개당 2,000원이 추가됩니다.</small>
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
      <label>단독 구매 무료배송 수량<input name="free_shipping_quantity" type="number" min="0" max="99" value="${Number(item.free_shipping_quantity || 0)}"><small>0은 사용 안 함 · 다른 상품을 함께 결제하면 적용되지 않습니다.</small></label>
      <h3>선택 항목 <small>최대 3개</small></h3>
      ${optionGroupEditors(item)}
      <small>선택 항목은 반드시 1번부터 순서대로 만드세요. 구매자 직접입력 질문을 선택하면 작성한 질문에 구매자가 장바구니에 담기 전에 답변합니다. 선택형 항목값에 (+2000)을 붙이면 개당 2,000원이 추가됩니다.</small>
      <label class="check"><input name="is_active" type="checkbox" ${item.is_active !== false ? "checked" : ""}> 판매 활성화</label>`;
  }
  $("#editor-form button[type='submit']").textContent = isLinkImport ? "링크에서 가져오기" : "저장";
  $("#editor").hidden = false;
  document.body.classList.add("modal-open");
  if (type === "product") {
    const optionGroups = $$('[data-option-group]', fields);
    const syncOptionGroups = () => optionGroups.forEach((group, index) => {
      const typeSelect = $('[data-group-label]', group);
      const customField = $('[data-group-custom]', group);
      const customInput = $('[data-group-custom-label]', group);
      const questionField = $('[data-group-question]', group);
      const questionInput = $('[data-group-question-value]', group);
      const optionsField = $('[data-group-options]', group);
      const optionsInput = $('[data-group-values]', group);
      const previousEnabled = index === 0 || $('[data-group-label]', optionGroups[index - 1]).value !== "없음";
      if (!previousEnabled) typeSelect.value = "없음";
      typeSelect.disabled = !previousEnabled;
      group.classList.toggle("locked", !previousEnabled);
      const unused = typeSelect.value === "없음";
      const custom = typeSelect.value === CUSTOM_OPTION_LABEL;
      const textQuestion = typeSelect.value === TEXT_OPTION_LABEL;
      customField.hidden = !custom;
      customInput.disabled = !custom;
      customInput.required = custom;
      questionField.hidden = !textQuestion;
      questionInput.disabled = !textQuestion;
      questionInput.required = textQuestion;
      optionsField.hidden = unused || textQuestion;
      optionsInput.disabled = unused || textQuestion;
      optionsInput.required = !unused && !textQuestion;
    });
    optionGroups.forEach(group => $('[data-group-label]', group).addEventListener("change", syncOptionGroups));
    syncOptionGroups();
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
    if (type === "product") {
      data.option_groups = $$('[data-option-group]', fields).map(group => {
        const selectedType = $('[data-group-label]', group).value;
        if (selectedType === TEXT_OPTION_LABEL) return {
          type: "text",
          label: $('[data-group-question-value]', group).value.trim(),
          options: [],
        };
        return {
          label: selectedType === CUSTOM_OPTION_LABEL ? $('[data-group-custom-label]', group).value.trim() : selectedType,
          options: $('[data-group-values]', group).disabled ? "" : $('[data-group-values]', group).value,
        };
      });
    }
    if (type === "product" && !isLinkImport) data.is_active = Boolean(data.is_active);
    const submit = event.target.querySelector('button[type="submit"]');
    const originalLabel = submit.textContent;
    submit.disabled = true;
    submit.textContent = isLinkImport ? "상품 가져오는 중..." : "저장 중...";
    try {
      const endpoint = type === "category"
        ? "/api/admin/categories"
        : isLinkImport ? "/api/admin/products/import" : state.productEditorScope === "subadmin" ? "/api/subadmin/products" : "/api/admin/products";
      const result = await api(endpoint, { method: "POST", body: JSON.stringify(data) });
      closeEditor();
      if (type === "product" && state.productEditorScope === "subadmin") await loadSubadminPanel();
      else await loadAdmin();
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
