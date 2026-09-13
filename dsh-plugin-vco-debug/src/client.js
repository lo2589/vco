// dsh-plugin-vco-debug — Client half.
//
// Format: window.__ModuleLoader__.load({ id, factory(require) }) with the id
// EQUAL to the PACKAGE name (`dsh-plugin-vco-debug`) — the bundle route is built
// from the package name, matching the working `dsh-provider-quick-config` bundle
// in this profile. The cordis entry id (`vco-debug`, in cordis.patch.yml) is a
// host-side identity and is not what this registers under. Externalize React
// through `require('react')`; plain React.createElement everywhere, no JSX.
//
// This half registers exactly one seat: `conversation.view`, so vco debug is a
// peer of Chat / Trajectory / CDD Lens. Because the bundle loads at BOOT (not
// through the dynamic-package path), two earlier obstacles disappear with it:
//   * a boot entry sorts by `order` (the dynamic path forced a negative
//     `priority`, which always sorted it first and made `order` irrelevant);
//   * the view-tabs list is snapshotted at bootstrap, and a boot registration is
//     present in time — a late dynamic one never was.
//
// ONE STEP, NOT TWO: the panel's own button says 「插入对话」, so a message that
// arrives from it goes straight into the composer. There is no confirmation
// click here — the draft is the review surface, and the user sends it (or edits
// it) in the composer like any other draft. The footer is a status line plus two
// optional verbs (send now / dismiss), never a gate in front of the insert.
//
// Measured contract (ui-conversation contract/input.ts): setDraft(text) REPLACES
// the whole draft and there is no append verb, so this half mirrors the live
// draft from `useInput` and writes back `existing + payload`. vco's side sends
// the annotation as its own field; this half owns turning "DOM info +
// annotation" into the text that lands in the composer.

window.__ModuleLoader__.load({
  id: 'dsh-plugin-vco-debug',
  factory: (require) => {
    const module = { exports: {} }
    const exports = module.exports
    const React = require('react')

    const CSS = [
      '.vco-page{display:flex;flex-direction:column;height:100%;min-height:0;',
      'width:100%;box-sizing:border-box;overflow:hidden;',
      'background:var(--dsw-alias-bg-layer-1,#fff);',
      'color:var(--dsw-alias-label-primary,#111);',
      'font-family:var(--dsw-font-family,inherit);font-size:12px}',
      '.vco-bar{display:flex;align-items:center;gap:6px;padding:6px 10px;',
      'border-bottom:1px solid var(--dsw-alias-border-secondary,#e5e7eb);flex:0 0 auto}',
      '.vco-dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;flex:0 0 auto}',
      '.vco-dot.on{background:#22c55e}',
      '.vco-dot.bad{background:#ef4444}',
      '.vco-bar .vco-title{flex:1;min-width:0;white-space:nowrap;overflow:hidden;',
      'text-overflow:ellipsis;font:11px ui-monospace,monospace;opacity:.75}',
      '.vco-bar button{padding:2px 9px;border-radius:5px;cursor:pointer;',
      'border:1px solid var(--dsw-alias-border-secondary,#d1d5db);',
      'background:transparent;color:inherit;font:11px var(--dsw-font-family,inherit)}',
      '.vco-bar button:hover{background:rgba(127,127,127,.12)}',
      '.vco-body{position:relative;flex:1;min-height:0}',
      '.vco-frame{position:absolute;left:0;top:0;width:100%;height:100%;border:0;display:block;background:#fff}',
      '.vco-hint{padding:16px;line-height:1.85;opacity:.7}',
      '.vco-hint code{background:rgba(127,127,127,.16);border-radius:3px;',
      'padding:1px 6px;font:11px ui-monospace,monospace}',
      '.vco-foot{display:flex;align-items:center;gap:10px;padding:4px 10px;',
      'border-top:1px solid var(--dsw-alias-border-secondary,#e5e7eb);flex:0 0 auto;',
      'overflow-x:auto;font:11px ui-monospace,monospace}',
      '.vco-foot .note{flex:0 0 auto;color:#16a34a;white-space:nowrap}',
      '.vco-row{display:flex;align-items:center;gap:4px;flex:0 0 auto;max-width:240px;',
      'border:1px solid var(--dsw-alias-border-secondary,#d1d5db);border-radius:5px;',
      'padding:1px 2px 1px 6px;background:rgba(127,127,127,.06)}',
      '.vco-row .txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;',
      'color:var(--dsw-alias-label-secondary,#374151)}',
      '.vco-row .del{border:0;background:transparent;color:#6b7280;cursor:pointer;',
      'font:13px/1 -apple-system;padding:0 3px;border-radius:3px}',
      '.vco-row .del:hover{color:#ef4444;background:rgba(239,68,68,.1)}',
      '.vco-row .del:disabled{color:#cbd5e1;cursor:default;background:transparent}',
      '.vco-row.gone{opacity:.45}',
      '.vco-row.gone .txt{text-decoration:line-through}',
    ].join('')

    function apply(ctx) {
      const slots = ctx.get('slots')
      if (slots === undefined) return

      ctx.effect(() => {
        const tag = document.createElement('style')
        tag.dataset.plugin = 'dsh-plugin-vco-debug'
        tag.dataset.pluginCss = 'dsh-plugin-vco-debug/main'
        tag.textContent = CSS
        document.head.appendChild(tag)
        return () => { tag.remove() }
      }, 'vco-debug:styles')

      // The panel is served by vco on its own origin, so its buttons reach this
      // page through postMessage. Only the panel's own message shape is accepted.
      //
      // The address comes from the Host half's same-origin route. The port probe
      // is a fallback for the case where that route is missing (a Host half that
      // applies before webServer exists), and it identifies the panel by its own
      // document marker rather than by an open port.
      const CANDIDATES = [8919, 8767, 8800]

      async function probePort(port) {
        try {
          const r = await fetch('http://127.0.0.1:' + port + '/', { cache: 'no-store' })
          if (!r.ok) return false
          const html = await r.text()
          return html.includes('vco-debug-panel')
        } catch (e) { return false }
      }

      // What actually lands in the composer: the DOM info the panel sent, with
      // the annotation in front of it. `info` payloads already open with the
      // note (the panel's own detailBlock puts it first), so a payload that
      // starts with the note is passed through untouched instead of doubled.
      function composeInsertText(text, annotation) {
        const body = String(text || '').trim()
        const note = String(annotation || '').trim()
        if (note === '') return body
        if (body === '') return note
        if (body.indexOf(note) === 0) return body
        return note + '\n\n' + body
      }

      function VcoView(props) {
        const [url, setUrl] = React.useState('')
        const [loaded, setLoaded] = React.useState(false)
        const [failed, setFailed] = React.useState(false)
        const [note, setNote] = React.useState('')
        // What this view has put into the composer, newest last. It exists so
        // an inserted block can be taken back out again: the composer is one
        // text draft, so a delete has to name the exact block it removes.
        const [added, setAdded] = React.useState([])
        const frameRef = React.useRef(null)
        const actions = props && props.inputActions
        // The live draft, mirrored so an insert appends to what is already
        // typed: the machine's setDraft REPLACES the whole draft, and a pick
        // written over a half-typed question would silently delete it. The
        // mirror is read by the message handler, which outlives any one render.
        const useInput = props && props.useInput
        const draft = typeof useInput === 'function'
          ? useInput((state) => (state ? state.draft : ''))
          : ''
        const draftRef = React.useRef('')
        draftRef.current = draft
        // The block this view wrote last, so re-picking the same element after
        // editing its annotation UPDATES that block instead of stacking a second
        // copy of the same element into the draft.
        const lastBlock = React.useRef('')

        React.useEffect(() => {
          let alive = true
          let lastUrl = ''
          const resolve = async () => {
            if (!alive) return
            // 1. Ask the Host half.
            try {
              const r = await fetch('/plugins/vco-debug/status', { cache: 'no-store' })
              if (r.ok) {
                const out = await r.json()
                if (!alive) return
                if (out && out.port) {
                  const next = 'http://127.0.0.1:' + out.port + '/'
                  if (next !== lastUrl) { lastUrl = next; setUrl(next) }
                  return
                }
                if (out && out.error) setNote(String(out.error).slice(0, 200))
              }
            } catch (e) { /* fall through to the probe */ }
            // 2. Probe the known ports.
            for (const port of CANDIDATES) {
              if (!alive) return
              if (await probePort(port)) {
                const next = 'http://127.0.0.1:' + port + '/'
                if (next !== lastUrl) { lastUrl = next; setUrl(next) }
                return
              }
            }
          }
          void resolve()
          // Always re-resolve on the interval — host self-heals every 15s and may
          // pick a new port; a once-and-stale `url` would leave the iframe
          // pointing at a SIGKILL'd process. The 5s cadence catches the swap
          // well before any user-perceived "disconnect".
          const id = ctx.interval(() => { void resolve() }, 5000)
          return () => { alive = false; if (typeof id === 'function') id() }
        }, [])

        // The one place a pick becomes composer text. Returns what happened so
        // the caller can report it; never asks the user to confirm first.
        const putInDraft = (text) => {
          if (!actions) return 'no-actions'
          if (text === '') return 'empty'
          const existing = String(draftRef.current || '')
          const prev = String(lastBlock.current || '')
          let next
          if (prev !== '' && prev !== text && existing.indexOf(prev) >= 0) {
            // Same element, edited annotation: replace the old block in place.
            next = existing.replace(prev, text)
          } else if (existing.indexOf(text) >= 0) {
            return 'already'
          } else {
            next = existing.trim() === '' ? text : existing.replace(/\s+$/, '') + '\n\n' + text
          }
          actions.setDraft(next)
          draftRef.current = next
          lastBlock.current = text
          return 'inserted'
        }

        /**
         * Take one block back out of the composer.
         *
         * The composer is a plain text draft, so a block leaves it the same way
         * it entered: by rewriting the draft without it. Both the trailing
         * separator and the block itself go, or deleting the last of several
         * blocks would leave a blank gap behind.
         */
        const removeFromDraft = (text) => {
          if (!actions) return false
          const existing = String(draftRef.current || '')
          if (existing.indexOf(text) < 0) return false
          const next = existing
            .split(text).join('')
            .replace(/\n{3,}/g, '\n\n')
            .replace(/^\s+|\s+$/g, '')
          actions.setDraft(next)
          draftRef.current = next
          if (lastBlock.current === text) lastBlock.current = ''
          return true
        }

        React.useEffect(() => {
          if (typeof window === 'undefined') return undefined
          const onMessage = (event) => {
            const data = event && event.data
            if (!data || typeof data !== 'object') return
            if (data.source !== 'vco-debug-panel') return
            if (typeof data.text !== 'string' || !data.text.trim()) return
            const annotation = typeof data.annotation === 'string' ? data.annotation : ''
            const next = {
              kind: data.kind === 'info' ? 'info' : 'insert-text',
              text: data.text,
              selector: typeof data.selector === 'string' ? data.selector : '',
              annotation: annotation,
            }
            // Straight in: the panel's button already said 「插入对话」, so the
            // draft is where it goes. No second click, no confirmation bar.
            const composed = composeInsertText(next.text, annotation)
            const outcome = putInDraft(composed)
            setNote(outcome === 'inserted' ? '已插入输入框'
              : outcome === 'already' ? '输入框里已经有这段了'
                : outcome === 'no-actions' ? '拿不到输入框（inputActions 缺失）'
                  : '内容是空的')
            if (outcome === 'inserted' || outcome === 'already') {
              // Same element re-sent (annotation edited): replace its entry
              // rather than stacking a second row for the same block.
              setAdded((list) => {
                const rest = list.filter((b) => b.selector !== next.selector || next.selector === '')
                return rest.concat([{ text: composed, selector: next.selector, at: Date.now() }])
              })
            }
          }
          window.addEventListener('message', onMessage)
          return () => window.removeEventListener('message', onMessage)
        }, [])

        React.useEffect(() => {
          if (!note) return undefined
          return ctx.timeout(() => setNote(''), 2600)
        }, [note])

        const dot = 'vco-dot' + (loaded ? ' on' : (failed ? ' bad' : ''))
        const bar = React.createElement('div', { className: 'vco-bar' },
          React.createElement('span', { className: dot }),
          React.createElement('span', { className: 'vco-title' },
            url + (loaded ? '  ·  已连上' : (failed ? '  ·  没响应' : '  ·  连接中…'))
            + (added.length ? '  ·  已插入 ' + added.length : '')),
        )

        const body = !url
          ? React.createElement('div', { className: 'vco-hint' },
              React.createElement('div', null, '面板还没起来。Host 半边会自己拉起它；也可以手动跑：'),
              React.createElement('div', { style: { marginTop: '6px' } },
                React.createElement('code', null, 'python3 -m vco.cli debug --port 8919')),
              note ? React.createElement('div', { style: { marginTop: '6px' } }, note) : null)
          : React.createElement('div', { className: 'vco-body' },
              React.createElement('iframe', {
                className: 'vco-frame',
                ref: frameRef,
                src: url,
                onLoad: () => { setLoaded(true); setFailed(false) },
                onError: () => { setFailed(true) },
              }))

        /**
         * One inserted block, with the box that takes it back out.
         *
         * A block that IS in the composer gets a live × that rewrites the draft
         * without it. A block whose text is no longer in the draft (deleted by
         * hand, or the draft was replaced) is shown struck through with an
         * inert box — the row still says what went in, but there is nothing
         * left to remove, and pretending otherwise would silently do nothing.
         */
        const renderBlock = (block) => {
          const live = String(draft).indexOf(block.text) >= 0
          const label = block.selector || block.text.split('\n')[0].slice(0, 48)
          return React.createElement('div', {
            className: 'vco-row' + (live ? '' : ' gone'),
            key: block.selector + '@' + block.at,
          },
            React.createElement('span', { className: 'txt', title: block.text }, label),
            React.createElement('button', {
              className: 'del',
              disabled: !live,
              title: live ? '从输入框里删掉这段' : '输入框里已经没有这段了',
              onClick: () => {
                if (!live) return
                const ok = removeFromDraft(block.text)
                setNote(ok ? '已从输入框删除' : '输入框里已经没有这段了')
                if (ok) setAdded((list) => list.filter((b) => b.at !== block.at))
              },
            }, '×'),
          )
        }

        const foot = (added.length || note)
          ? React.createElement('div', { className: 'vco-foot' },
              added.map(renderBlock),
              note ? React.createElement('span', { className: 'note' }, note) : null)
          : null

        return React.createElement('div', { className: 'vco-page' }, bar, body, foot)
      }

      // order 30: after chat (0), trajectory (10) and CDD Lens (20). A boot
      // entry keeps priority 0, so `order` is what decides this.
      slots.inject('conversation.view', () => slots.register({
        name: 'conversation.view',
        id: 'vco',
        order: 30,
        label: 'VCO',
      }, (props) => React.createElement(VcoView, props)))
    }

    module.exports = { name: 'vco-debug', inject: ['slots', 'timer'], apply }
    return module.exports
  },
})
