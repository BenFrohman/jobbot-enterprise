/* Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.
 * Proprietary and confidential. No use, copying or distribution without written permission.
 *
 * Jobbot careers widget. Embed:
 *   <div id="jobbot"></div>
 *   <script src="https://jobs.internal.example.com/widget.js"
 *           data-api="https://jobs.internal.example.com" data-key="jbp_..." data-target="#jobbot"></script>
 * Uses a publishable widget key (role "widget"), which can only list open roles and submit applications.
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var API = (script && script.getAttribute("data-api")) || "";
  var KEY = (script && script.getAttribute("data-key")) || "";
  var root = document.querySelector((script && script.getAttribute("data-target")) || "#jobbot");
  if (!root || !KEY) return;
  var LABELS = { name: "Name", email: "Email", phone: "Phone", location: "Location", links: "Links",
    skills: "Skills", experience: "Experience", values: "What you value", causes: "Causes you care about",
    desired_impact: "The impact you want to have", comp_expectation: "Compensation expectation",
    resume_text: "Resume (paste text)" };
  var LONG = { experience: 1, values: 1, desired_impact: 1, resume_text: 1 };

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k]; else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { e.appendChild(c); });
    return e;
  }
  function call(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Authorization": "Bearer " + KEY, "Content-Type": "application/json" }, opts.headers || {});
    return fetch(API + path, opts).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || r.status); return j; });
    });
  }

  function form(job, cfg) {
    var f = el("form", { "class": "jb-form" });
    cfg.ask_fields.forEach(function (k) {
      var input = el(LONG[k] ? "textarea" : "input", { name: k, "aria-label": LABELS[k] || k });
      if (k === "email") { input.setAttribute("type", "email"); input.required = true; }
      if (k === "comp_expectation") input.setAttribute("type", "number");
      f.appendChild(el("label", {}, [el("span", { text: LABELS[k] || k }), input]));
    });
    var box = el("fieldset", {}, [el("legend", { text: "You choose what " + (cfg.company_name || "the company") + " can see" })]);
    Object.keys(LABELS).forEach(function (k) {
      var cb = el("input", { type: "checkbox", name: "consent", value: k });
      cb.checked = cfg.default_consent.indexOf(k) >= 0;
      box.appendChild(el("label", { "class": "jb-consent" }, [cb, el("span", { text: LABELS[k] })]));
    });
    f.appendChild(box);
    var msg = el("p", { "class": "jb-msg", role: "status" });
    f.appendChild(el("button", { type: "submit", text: "Apply" }));
    f.appendChild(msg);
    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var profile = {}, grant = [];
      Array.prototype.forEach.call(f.elements, function (x) {
        if (x.name === "consent") { if (x.checked) grant.push(x.value); }
        else if (x.name) profile[x.name] = x.name === "comp_expectation" ? (x.value ? +x.value : null) : x.value;
      });
      call("/v1/public/applications", { method: "POST",
        body: JSON.stringify({ posting_id: job.id, profile: profile, consent_fields: grant }) })
        .then(function (r) {
          try { localStorage.setItem("jobbot_candidate_token", r.candidate_token); } catch (e) {}
          f.replaceChildren(el("p", { text: "Application sent. You can change what you share at any time." }));
        })
        .catch(function (e) { msg.textContent = "Could not apply: " + e.message; });
    });
    return f;
  }

  Promise.all([call("/v1/public/widget/config"), call("/v1/public/postings")]).then(function (res) {
    var cfg = res[0], jobs = res[1];
    root.style.setProperty("--jb-accent", cfg.accent_color);
    var list = el("div", { "class": "jb-board" });
    if (cfg.intro) list.appendChild(el("p", { text: cfg.intro }));
    if (!jobs.length) list.appendChild(el("p", { text: "No open roles right now." }));
    jobs.forEach(function (j) {
      var meta = [j.team, j.location, j.remote ? "Remote" : ""].filter(Boolean).join(" · ");
      var card = el("article", { "class": "jb-job" }, [el("h3", { text: j.title }), el("p", { text: meta }),
        el("p", { text: (j.description || "").slice(0, 400) })]);
      var btn = el("button", { type: "button", text: "Apply" });
      btn.addEventListener("click", function () { btn.replaceWith(form(j, cfg)); });
      card.appendChild(btn);
      list.appendChild(card);
    });
    var css = el("style", { text: ".jb-job{border:1px solid #ddd;border-radius:8px;padding:12px;margin:8px 0}" +
      ".jb-job button,.jb-form button{background:var(--jb-accent);color:#fff;border:0;border-radius:6px;padding:8px 14px}" +
      ".jb-form label{display:block;margin:6px 0}.jb-form input,.jb-form textarea{width:100%}" +
      ".jb-consent{display:inline-block!important;margin-right:12px!important}" });
    root.replaceChildren(css, list);
  }).catch(function (e) { root.textContent = "Careers board unavailable: " + e.message; });
})();
