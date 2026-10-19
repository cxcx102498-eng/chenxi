// Clean image-led whiteboard: original media + timed explanation, no paper filter.
// image.data.box: normalized inside the media area; annotation rect: inside original image.
CLIPS.y4_media_board = (() => {
  const C0 = { background: '#FFFFFF', ink: '#0B214A', accent: '#FF7A00', blue: '#1597F5' };
  const clamp = n => Math.max(0, Math.min(1, n));
  const ease = p => { p = clamp(p); return p * p * (3 - 2 * p); };
  const mix = (a, b, p) => a + (b - a) * p;
  let media, regions, color;

  function latest(cues, t) {
    return cues.filter(q => q.at <= t + 1e-8 && (q.dur == null || t < q.at + q.dur - 1e-8)).pop();
  }
  function transformAt(item, t) {
    let out = item.base;
    for (const event of item.moves) {
      if (t < event.at) break;
      const p = ease((t - event.at) / event.dur);
      out = { x: mix(event.from.x, event.to.x, p), y: mix(event.from.y, event.to.y, p), s: mix(event.from.s, event.to.s, p) };
    }
    return out;
  }
  // A short entry motion, then a stable image. Annotations use this same transform.
  function entryAt(item, tr, t, ctx) {
    const mode = item.q.data?.enter ?? ctx.data.image_enter ?? 'fade-up';
    if (mode === 'none') return { ...tr, alpha: 1 };
    const dur = Math.min(item.q.data?.enter_duration ?? ctx.data.image_enter_duration ?? 0.6, ctx.dur - item.q.at);
    const p = ease((t - item.q.at + 1 / ctx.FPS) / dur);
    if (mode === 'zoom-in') {
      const k = mix(0.96, 1, p);
      return { x: tr.x + item.im.width * tr.s * (1-k)/2,
        y: tr.y + item.im.height * tr.s * (1-k)/2, s: tr.s*k, alpha: p };
    }
    return { ...tr, y: tr.y + (mode === 'fade-up' ? 24 * ctx.u * (1-p) : 0), alpha: p };
  }
  function fittedText(g, text, box, size, family, col, maxLines = 2, align = 'left') {
    if (!text) return;
    const fit = TY.fit(g, String(text), box.w, size, family, { maxLines, min: 0.7, soft: true, balance: true });
    if (!fit.ok || fit.lines.length * fit.size * 1.35 > box.h + 1) {
      throw new Error(`y4_media_board text does not fit its reserved region; shorten or split: ${String(text).slice(0, 60)}`);
    }
    g.save(); g.beginPath(); g.rect(box.x, box.y, box.w, box.h); g.clip();
    g.font = `${fit.size}px "${family}"`; g.fillStyle = col; g.textAlign = align; g.textBaseline = 'middle';
    const lineH = fit.size * 1.35, y = box.y + (box.h - lineH * fit.lines.length) / 2 + lineH / 2;
    const x = align === 'center' ? box.x + box.w / 2 : box.x;
    fit.lines.forEach((line, i) => g.fillText(line, x, y + i * lineH));
    g.restore();
  }
  function annotation(g, q, item, tr, t, ctx) {
    if (q.at > t + 1e-8) return;
    const r = q.data.rect, x = tr.x + r[0] * item.im.width * tr.s, y = tr.y + r[1] * item.im.height * tr.s;
    const w = r[2] * item.im.width * tr.s, h = r[3] * item.im.height * tr.s;
    const p = ease((t - q.at + 1 / ctx.FPS) / (q.dur || 0.55));
    const style = q.data.style || 'rect';
    g.save(); g.strokeStyle = color.accent; g.lineWidth = 4.5 * ctx.u; g.lineCap = 'round'; g.lineJoin = 'round';
    g.beginPath();
    if (style === 'circle') {
      g.ellipse(x + w / 2, y + h / 2, Math.max(0.1, w / 2), Math.max(0.1, h / 2), 0, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * p);
    } else if (style === 'underline') {
      g.moveTo(x, y + h); g.lineTo(x + w * p, y + h);
    } else {
      const points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]];
      let left = (w + h) * 2 * p;
      g.moveTo(x, y);
      for (let i = 1; i < points.length && left > 0; i++) {
        const a = points[i - 1], b = points[i], length = Math.hypot(b[0] - a[0], b[1] - a[1]);
        const f = Math.min(1, left / length); g.lineTo(mix(a[0], b[0], f), mix(a[1], b[1], f)); left -= length;
      }
    }
    g.stroke(); g.restore();
  }
  function sourceRow(g, item, t, ctx) {
    if (!item.caption.h) return;
    let textBox = { ...item.caption };
    if (item.hasFocus) {
      const thumbW = Math.min(item.caption.w * 0.22, 100 * ctx.u), pad = 5 * ctx.u;
      const thumb = CLIP.fit(item.im.width, item.im.height, item.caption.x + pad, item.caption.y + pad, thumbW - 2 * pad, item.caption.h - 2 * pad);
      g.drawImage(item.im, thumb.x, thumb.y, thumb.w, thumb.h);
      g.save(); g.strokeStyle = color.blue; g.lineWidth = 1.5 * ctx.u; g.strokeRect(thumb.x, thumb.y, thumb.w, thumb.h);
      const current = item.moves.filter(e => e.at <= t).pop();
      if (current && current.rect) { const r = current.rect; g.strokeStyle = color.accent; g.strokeRect(thumb.x + r[0] * thumb.w, thumb.y + r[1] * thumb.h, r[2] * thumb.w, r[3] * thumb.h); }
      g.restore();
      textBox.x += thumbW + 12 * ctx.u; textBox.w -= thumbW + 12 * ctx.u;
    }
    fittedText(g, item.captionText, textBox, 26 * ctx.u, 'PuHui-Medium', color.ink, 1);
  }
  return {
    fonts: ['PuHui-Bold', 'PuHui-Medium'],
    safe: true,
    drawsSafeBand: true,
    init(ctx) {
      const { box: b, u } = ctx;
      color = { ...C0, ...ctx.theme };
      const pad = 24 * u, gap = 20 * u;
      for (const key of ['reserve_title', 'reserve_point']) {
        if (key in ctx.data && typeof ctx.data[key] !== 'boolean') throw new Error(`data.${key} must be boolean`);
      }
      // A state sequence may reserve an empty text band so its diagram never jumps.
      const titleH = ctx.of('title').length || ctx.data.draft || ctx.data.reserve_title === true ? 150 * u : 0;
      const pointH = ctx.of('image').length && (ctx.of('point').length || ctx.data.reserve_point === true) ? 126 * u : 0;
      const draftW = ctx.data.draft ? 330 * u : 0;
      const top = b.y + pad, mediaY = top + titleH + (titleH ? gap : 0);
      const mediaH = b.h - 2 * pad - titleH - pointH - (titleH ? gap : 0) - (pointH ? gap : 0);
      if (b.w <= 2 * pad || mediaH <= 80 * u) throw new Error('y4_media_board: safe margins leave insufficient media space');
      regions = {
        title: { x: b.x + pad, y: top, w: b.w - 2 * pad - draftW, h: titleH },
        media: { x: b.x + pad, y: mediaY, w: b.w - 2 * pad, h: mediaH },
        point: { x: b.x + pad, y: mediaY + mediaH + gap, w: b.w - 2 * pad, h: pointH },
        subtitle: { x: b.x + pad, y: ctx.H - ctx.safe.bottom + 14 * u, w: b.w - 2 * pad, h: ctx.safe.bottom - 28 * u },
      };
      if (!ctx.of('image').length) regions.point = { ...regions.media };
      media = ctx.of('image').map(q => {
        const im = ctx.IMG[q.image], r = q.data?.box || [0, 0, 1, 1], a = regions.media;
        const target = { x: a.x + r[0] * a.w, y: a.y + r[1] * a.h, w: r[2] * a.w, h: r[3] * a.h };
        const hasFocus = ctx.of('focus').some(e => e.data.asset_id === q.asset_id);
        // Old specs may still carry attribution in q.text. It is hidden unless explicitly enabled.
        const captionText = [ctx.data.show_image_labels === true ? q.data?.label : '', ctx.data.show_source_credits === true ? q.text : ''].filter(Boolean).join(' · ');
        const captionH = hasFocus ? 68 * u : captionText ? 48 * u : 0;
        const imageBox = { ...target, h: target.h - captionH };
        if (imageBox.h <= 32 * u) throw new Error('y4_media_board: image box is too short after source label');
        const fit = CLIP.fit(im.width, im.height, imageBox.x, imageBox.y, imageBox.w, imageBox.h);
        const item = { q, im, imageBox, captionText, base: { x: fit.x, y: fit.y, s: fit.s }, hasFocus, moves: [],
          caption: { x: target.x, y: target.y + imageBox.h, w: target.w, h: captionH },
          highlights: ctx.of('highlight').filter(e => e.data.asset_id === q.asset_id) };
        for (const e of ctx.of('focus', 'reset').filter(e => e.data.asset_id === q.asset_id)) {
          let to = item.base;
          if (e.kind === 'focus') {
            const r = e.data.rect, inset = 16 * u;
            const s = Math.min((imageBox.w - 2 * inset) / (im.width * r[2]), (imageBox.h - 2 * inset) / (im.height * r[3]));
            to = { s, x: imageBox.x + imageBox.w / 2 - (r[0] + r[2] / 2) * im.width * s,
              y: imageBox.y + imageBox.h / 2 - (r[1] + r[3] / 2) * im.height * s };
          }
          item.moves.push({ at: e.at, dur: e.dur || 0.65, from: transformAt(item, e.at), to, rect: e.kind === 'focus' ? e.data.rect : null });
        }
        return item;
      });
    },
    draw(g, t, ctx) {
      if (!ctx.alpha) {
        g.fillStyle = color.background; g.fillRect(0, 0, ctx.W, ctx.H);
        if (ctx.safe.fill) {
          g.fillStyle = ctx.safe.fill;
          const s = ctx.safe;
          g.fillRect(0, 0, ctx.W, s.top); g.fillRect(0, ctx.H - s.bottom, ctx.W, s.bottom);
          g.fillRect(0, 0, s.left, ctx.H); g.fillRect(ctx.W - s.right, 0, s.right, ctx.H);
        }
      }
      for (const item of media) {
        if (t < item.q.at - 1e-8) continue;
        const tr = entryAt(item, transformAt(item, t), t, ctx), b = item.imageBox;
        g.save(); g.globalAlpha *= tr.alpha;
        g.save(); g.beginPath(); g.rect(b.x, b.y, b.w, b.h); g.clip();
        g.drawImage(item.im, tr.x, tr.y, item.im.width * tr.s, item.im.height * tr.s);
        for (const q of item.highlights) annotation(g, q, item, tr, t, ctx);
        g.restore(); sourceRow(g, item, t, ctx); g.restore();
      }
      const title = latest(ctx.of('title'), t), point = latest(ctx.of('point'), t), subtitle = latest(ctx.of('subtitle'), t);
      if (title) fittedText(g, title.text, regions.title, 54 * ctx.u, 'PuHui-Bold', color.ink);
      if (point) fittedText(g, point.text, regions.point, (media.length ? 43 : 58) * ctx.u, 'PuHui-Bold', color.blue, media.length ? 2 : 4, media.length ? 'left' : 'center');
      if (subtitle) fittedText(g, subtitle.text, regions.subtitle, 40 * ctx.u, 'PuHui-Medium', color.ink, 2, 'center');
      if (ctx.data.draft) fittedText(g, ctx.data.watermark || '预览草稿', { x: ctx.box.x + ctx.box.w - 330 * ctx.u, y: regions.title.y, w: 306 * ctx.u, h: 60 * ctx.u }, 28 * ctx.u, 'PuHui-Medium', color.accent, 1, 'center');
    },
  };
})();
