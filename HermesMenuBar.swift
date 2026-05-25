import Cocoa
import Foundation

// ── Config ────────────────────────────────────────────────────────────────────
let RAILWAY_URL   = "https://web-production-1f715.up.railway.app/dashboard/index.html"
let LOCAL_URL     = "http://127.0.0.1:8000/dashboard/index.html"
let BUILD_DIR     = "/Users/jefng3/.claude/projects/silicon-boardroom/builds/dashboards/i_want_to_create_a_beautiful_social_20260523_041316_677"
let VENV_UVICORN  = "/Users/jefng3/.claude/projects/silicon-boardroom/.venv/bin/uvicorn"
let VENV_PYTHON   = "/Users/jefng3/.claude/projects/silicon-boardroom/.venv/bin/python3"
let PROXY_SCRIPT  = BUILD_DIR + "/ollama_proxy.py"
let HERMES_TOKEN  = "hjHgAwa5HPgk53BKQYd9H7MPiwg6mJZisvuzSJnaRho"
let TUNNEL_CMD    = "/Users/jefng3/Desktop/Hermes-Tunnel.command"

// ── App Delegate ──────────────────────────────────────────────────────────────
class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var localServerPID: pid_t = 0
    var statusTimer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)   // no Dock icon
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "⚡ H"
        statusItem.button?.font = NSFont.monospacedSystemFont(ofSize: 13, weight: .semibold)
        buildMenu()
        // Poll status every 10s to refresh indicators
        statusTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            self?.buildMenu()
        }
    }

    // ── Status checks ─────────────────────────────────────────────────────────
    func isPortListening(_ port: Int) -> Bool {
        var hints = addrinfo()
        hints.ai_socktype = SOCK_STREAM
        var res: UnsafeMutablePointer<addrinfo>?
        let host = "127.0.0.1"
        let svc  = String(port)
        guard getaddrinfo(host, svc, &hints, &res) == 0, let ai = res else { return false }
        defer { freeaddrinfo(res) }
        let fd = socket(ai.pointee.ai_family, ai.pointee.ai_socktype, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var tv = timeval(tv_sec: 0, tv_usec: 300_000)
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        let connected = connect(fd, ai.pointee.ai_addr, ai.pointee.ai_addrlen) == 0
        return connected
    }

    func isOllamaUp()    -> Bool { isPortListening(11434) }
    func isLocalUp()     -> Bool { isPortListening(8000) }
    func isProxyUp()     -> Bool { isPortListening(11435) }
    func isTunnelUp()    -> Bool {
        guard let data = try? Data(contentsOf: URL(string: "http://127.0.0.1:4040/api/tunnels")!),
              let str  = String(data: data, encoding: .utf8) else { return false }
        return str.contains("public_url")
    }

    // ── Menu builder ──────────────────────────────────────────────────────────
    func buildMenu() {
        let menu = NSMenu()
        menu.autoenablesItems = false

        let ollamaUp = isOllamaUp()
        let localUp  = isLocalUp()
        let proxyUp  = isProxyUp()
        let tunnelUp = isTunnelUp()

        // Update icon to reflect worst status
        let allGood = ollamaUp && localUp
        statusItem.button?.title = allGood ? "⚡ H" : "⚡ H"
        statusItem.button?.contentTintColor = allGood ? .systemGreen : .systemOrange

        // ── Header ────────────────────────────────────────────────────────────
        addHeader(menu, "HERMES")

        // ── Status indicators ─────────────────────────────────────────────────
        addStatus(menu, "Ollama (11434)",  ollamaUp)
        addStatus(menu, "Local server (8000)", localUp)
        addStatus(menu, "Proxy (11435)",   proxyUp)
        addStatus(menu, "ngrok Tunnel",    tunnelUp)
        menu.addItem(.separator())

        // ── Open links ────────────────────────────────────────────────────────
        addHeader(menu, "OPEN")
        add(menu, title: "Local Dashboard",     key: "l") { NSWorkspace.shared.open(URL(string: LOCAL_URL)!) }
        add(menu, title: "Railway (production)",key: "r") { NSLog("Opening production URL: %@", RAILWAY_URL); NSWorkspace.shared.open(URL(string: RAILWAY_URL)!) }
        add(menu, title: "ngrok Inspector",     key: "n") { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:4040")!) }
        menu.addItem(.separator())

        // ── Start / stop local server ─────────────────────────────────────────
        addHeader(menu, "LOCAL SERVER")
        if localUp {
            add(menu, title: "Stop Local Server", key: "") { self.stopLocalServer() }
        } else {
            add(menu, title: "▶ Start Local Server", key: "s") { self.startLocalServer() }
        }
        menu.addItem(.separator())

        // ── Tunnel / proxy controls ───────────────────────────────────────────
        addHeader(menu, "RAILWAY TUNNEL")
        add(menu, title: "▶ Start Tunnel (Hermes-Tunnel)", key: "t") { self.startTunnel() }
        if proxyUp {
            add(menu, title: "Stop Proxy", key: "") { self.stopProxy() }
        }
        if tunnelUp {
            add(menu, title: "Stop ngrok", key: "") { self.stopNgrok() }
        }
        add(menu, title: "Copy ngrok URL to clipboard", key: "c") { self.copyNgrokURL() }
        menu.addItem(.separator())

        // ── Quit ──────────────────────────────────────────────────────────────
        add(menu, title: "Quit Hermes Menu Bar", key: "q") { NSApp.terminate(nil) }

        statusItem.menu = menu
    }

    // ── Actions ───────────────────────────────────────────────────────────────
    func startLocalServer() {
        guard !isLocalUp() else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: VENV_UVICORN)
        proc.arguments = ["api:app", "--host", "127.0.0.1", "--port", "8000"]
        proc.currentDirectoryURL = URL(fileURLWithPath: BUILD_DIR)
        proc.environment = ProcessInfo.processInfo.environment
        try? proc.run()
        localServerPID = proc.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            NSWorkspace.shared.open(URL(string: LOCAL_URL)!)
            self.buildMenu()
        }
    }

    func stopLocalServer() {
        // Kill by port
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/bin/bash")
        t.arguments = ["-c", "lsof -ti :8000 | xargs kill -9 2>/dev/null"]
        try? t.run(); t.waitUntilExit()
        buildMenu()
    }

    func startTunnel() {
        let commandURL = URL(fileURLWithPath: TUNNEL_CMD)
        guard FileManager.default.isExecutableFile(atPath: TUNNEL_CMD) else {
            showAlert(
                title: "Hermes Tunnel is not executable",
                message: "Run chmod +x \(TUNNEL_CMD), then try again."
            )
            return
        }

        let escapedPath = TUNNEL_CMD
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = """
        tell application "Terminal"
            activate
            do script quoted form of "\(escapedPath)"
        end tell
        """
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        t.arguments = ["-e", script]

        do {
            try t.run()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.buildMenu() }
        } catch {
            if NSWorkspace.shared.open(commandURL) {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.buildMenu() }
            } else {
                showAlert(title: "Could not open Hermes Tunnel", message: error.localizedDescription)
            }
        }
    }

    func stopProxy() {
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/bin/bash")
        t.arguments = ["-c", "pkill -f ollama_proxy.py 2>/dev/null"]
        try? t.run(); t.waitUntilExit()
        buildMenu()
    }

    func stopNgrok() {
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/bin/bash")
        t.arguments = ["-c", "pkill ngrok 2>/dev/null"]
        try? t.run(); t.waitUntilExit()
        buildMenu()
    }

    func copyNgrokURL() {
        guard let data = try? Data(contentsOf: URL(string: "http://127.0.0.1:4040/api/tunnels")!),
              let str  = String(data: data, encoding: .utf8),
              let range = str.range(of: "https://[^\"]+", options: .regularExpression) else {
            NSSound.beep(); return
        }
        let url = String(str[range])
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url, forType: .string)
    }

    // ── Menu helpers ──────────────────────────────────────────────────────────
    func addHeader(_ menu: NSMenu, _ title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 10, weight: .bold),
            .foregroundColor: NSColor.secondaryLabelColor
        ])
        item.isEnabled = false
        menu.addItem(item)
    }

    func addStatus(_ menu: NSMenu, _ label: String, _ up: Bool) {
        let dot  = up ? "● " : "○ "
        let item = NSMenuItem(title: dot + label, action: nil, keyEquivalent: "")
        item.attributedTitle = NSAttributedString(string: dot + label, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
            .foregroundColor: up ? NSColor.systemGreen : NSColor.systemOrange
        ])
        item.isEnabled = false
        menu.addItem(item)
    }

    func add(_ menu: NSMenu, title: String, key: String, action: @escaping () -> Void) {
        let item = NSMenuItem(title: title, action: #selector(menuAction(_:)), keyEquivalent: key)
        item.target = self
        item.representedObject = ActionWrapper(action)
        menu.addItem(item)
    }

    func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }

    @objc func menuAction(_ sender: NSMenuItem) {
        (sender.representedObject as? ActionWrapper)?.action()
    }
}

class ActionWrapper: NSObject {
    let action: () -> Void
    init(_ action: @escaping () -> Void) { self.action = action }
}

// ── Entry point ───────────────────────────────────────────────────────────────
let app      = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
