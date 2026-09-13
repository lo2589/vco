// dsh-plugin-vco-debug — Host half.
//
// Installed form: loaded from the web profile at boot, NOT through the dynamic
// package path. That difference is the whole point — this half runs in the real
// Cordis process, so `process`, `child_process`, `net` and `fs` exist. The
// dynamic sandbox had `process === undefined`, which is why the panel server
// previously had to be started by hand and went blank the moment it died.
//
// Entry-id contract: cordis.patch.yml sets `id: vco-debug` (the entry id used
// by the host-side entry tree) and `name: dsh-plugin-vco-debug` (the package
// name). The client half (src/client.js) must register with `id` equal to the
// package name — the module table keys factories by package name, not entry id.

const { spawn } = require('node:child_process')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')

const BASE_PORT = 8919
const MAX_PORT_TRIES = 8
const READY_TRIES = 30
const READY_INTERVAL_MS = 400

module.exports = {
  name: 'vco-debug',
  // `webServer` is a HARD dependency, not an optional `ctx.get`: a bundle loads
  // before that service exists, and Cordis only calls apply once every declared
  // dependency is present. Without this the status route silently never registers.
  inject: ['timer', 'webServer'],
  apply(ctx, config) {
    const state = { child: null, port: 0, url: '', startedAt: 0, lastError: '', log: [] }

    config = config || {}
    const root = typeof config.workspace === 'string' && config.workspace
      ? config.workspace
      : (process.env.VCO_HOME || process.cwd())

    // Where pick screenshots land. Inside the workspace, so the path written
    // into the chat is one the reader can actually open, and so the shot route
    // has a single directory to confine itself to.
    const SHOTS_DIR = path.resolve(root, '.vco-runtime', 'shots')
    try { fs.mkdirSync(SHOTS_DIR, { recursive: true }) } catch (e) { /* reported on write */ }

    function note(text) {
      state.log.push(String(text))
      while (state.log.length > 40) state.log.shift()
    }

    function snapshot(extra) {
      return Object.assign({
        running: alive(),
        url: state.url,
        port: state.port,
        pid: state.child ? state.child.pid : 0,
        error: state.lastError,
        log: state.log.slice(-8),
      }, extra || {})
    }

    /**
     * Is the panel process actually there?
     *
     * `child.exitCode === null` is not enough: a process killed from outside
     * (the interpreter's directory removed under it, or a SIGTERM from a shell)
     * can leave that null while the PID is long gone, and the status route then
     * reports a healthy panel that answers nothing. Ask the OS instead.
     */
    function alive() {
      const child = state.child
      if (!child || child.exitCode !== null || child.signalCode !== null) return false
      try {
        process.kill(child.pid, 0)
        return true
      } catch (e) {
        return false
      }
    }

    function resolvePython() {
      const candidates = []
      if (typeof config.python === 'string' && config.python) candidates.push(config.python)
      if (process.env.VCO_PYTHON) candidates.push(process.env.VCO_PYTHON)
      candidates.push(path.join(root, '.venv', 'bin', 'python'))
      candidates.push('/usr/bin/python3')
      candidates.push('/opt/homebrew/bin/python3')
      candidates.push('/usr/local/bin/python3')
      for (const c of candidates) {
        try { if (fs.existsSync(c)) return c } catch (e) { /* keep looking */ }
      }
      return 'python3'
    }

    /** The panel's own identity marker is the proof, so a stray server on the
     *  same port is never mistaken for the panel. */
    function probePanel(port) {
      return new Promise((resolve) => {
        let settled = false
        const done = (ok) => { if (!settled) { settled = true; resolve(ok) } }
        try {
          const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 900 }, (res) => {
            let body = ''
            res.setEncoding('utf8')
            res.on('data', (chunk) => { if (body.length < 8000) body += chunk })
            res.on('end', () => done(res.statusCode === 200 && body.includes('vco-debug-panel')))
          })
          req.on('error', () => done(false))
          req.on('timeout', () => { try { req.destroy() } catch (e) {} done(false) })
        } catch (e) { done(false) }
      })
    }

    function stop() {
      const child = state.child
      state.child = null
      state.url = ''
      state.port = 0
      if (!child) return
      try { child.kill('SIGTERM') } catch (e) { /* already gone */ }
      setTimeout(() => { try { child.kill('SIGKILL') } catch (e) {} }, 1500)
    }

    /** Bring the panel up: reuse one already answering, otherwise spawn it. */
    async function start(target) {
      if (alive()) return snapshot({ ok: true, note: '已在运行' })
      // A stale child record (dead PID) must not block a fresh start.
      if (state.child) { note('旧的进程记录已失效，重新启动'); state.child = null }

      const configured = Number(config.port) || BASE_PORT
      const python = resolvePython()
      state.lastError = ''

      for (let i = 0; i < MAX_PORT_TRIES; i++) {
        const port = configured + i
        if (await probePanel(port)) {
          state.port = port
          state.url = 'http://127.0.0.1:' + port + '/'
          state.startedAt = Date.now()
          note('复用已在运行的面板 :' + port)
          return snapshot({ ok: true, note: '复用已在运行的面板' })
        }

        const args = ['-m', 'vco.cli', 'debug', '--port', String(port),
                      '--profile', path.join(root, '.vco-runtime', 'profile')]
        const initial = target || (typeof config.target === 'string' ? config.target : '')
        if (initial) args.push(initial)
        let child
        try {
          child = spawn(python, args, {
            cwd: root,
            env: Object.assign({}, process.env, { PYTHONUNBUFFERED: '1' }),
            stdio: ['ignore', 'pipe', 'pipe'],
          })
        } catch (e) {
          state.lastError = 'spawn 失败: ' + String(e && e.message)
          return snapshot({ ok: false })
        }
        child.stdout.on('data', (buf) => note(String(buf).trim().slice(0, 200)))
        child.stderr.on('data', (buf) => note(String(buf).trim().slice(0, 200)))
        state.child = child
        state.port = port
        state.url = 'http://127.0.0.1:' + port + '/'
        state.startedAt = Date.now()

        for (let n = 0; n < READY_TRIES; n++) {
          if (child.exitCode !== null) break
          if (await probePanel(port)) return snapshot({ ok: true, note: '已启动' })
          await new Promise((r) => setTimeout(r, READY_INTERVAL_MS))
        }
        stop()
        state.lastError = '启动没成功（端口 ' + port + '）：' + (state.log[state.log.length - 1] || '无输出')
      }
      return snapshot({ ok: false })
    }

    // --- the Client half's one call ----------------------------------------
    // Plain JSON over HTTP: a couple of facts, nothing live, nothing large.
    const server = ctx.get('webServer')
    if (server !== undefined && typeof server.register === 'function') {
      ctx.effect(() => server.register({
        kind: 'exact',
        path: '/plugins/vco-debug/status',
        handler: (req, res) => {
          res.writeHead(200, {
            'content-type': 'application/json; charset=utf-8',
            'cache-control': 'no-store',
          })
          res.end(JSON.stringify(snapshot()))
        },
      }), 'vco-debug:status-route')

      // Screenshots the pick took, served from the SAME origin as the page.
      //
      // The panel and this route are on different origins, so the panel cannot
      // hand the chat a URL it can display; what it can hand over is the
      // absolute path (the reader has a filesystem) plus this route, which the
      // page CAN load. Paths are confined to the shots directory: no traversal
      // out of it, whatever a request asks for.
      ctx.effect(() => server.register({
        kind: 'prefix',
        path: '/plugins/vco-debug/shot/',
        handler: (req, res) => {
          const asked = decodeURIComponent(String(req.url || '').split('?')[0])
            .slice('/plugins/vco-debug/shot/'.length)
          const full = path.resolve(SHOTS_DIR, asked)
          if (!full.startsWith(SHOTS_DIR + path.sep) || !fs.existsSync(full)) {
            res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' })
            res.end('not found')
            return
          }
          try {
            const ext = path.extname(full).toLowerCase()
            const type = ext === '.jpg' || ext === '.jpeg' ? 'image/jpeg'
              : ext === '.webp' ? 'image/webp' : 'image/png'
            res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store' })
            res.end(fs.readFileSync(full))
          } catch (e) {
            res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' })
            res.end(String(e && e.message))
          }
        },
      }), 'vco-debug:shot-route')
    } else {
      note('webServer 不可用，客户端将自行探测面板端口')
    }

    ctx.effect(() => () => stop(), 'vco-debug:server')

    // Start eagerly, so the tab is useful the first time it is opened.
    void start()
    ctx.interval(() => {
      // Self-heal: if the panel died, bring it back rather than leaving a blank frame.
      if (!alive()) void start()
    }, 15000)
  },
}
