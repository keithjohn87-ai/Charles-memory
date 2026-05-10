// OtherViews.swift — Activity, Goals, System, Tools, Settings.
//
// Kept in one file to make the skeleton drag-drop simpler. Split out as
// they grow.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - Activity

struct ActivityView: View {
    @State private var entries: [ActivityEntry] = []
    @State private var error: String?

    var body: some View {
        List(entries) { e in
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(e.role.uppercased())
                        .font(.caption2.bold())
                    Text(e.convId)
                        .font(.caption2.monospaced())
                        .foregroundStyle(Color.bronzeIvoryDim)
                    Spacer()
                    Text(e.createdAt.prefix(19))
                        .font(.caption2)
                        .foregroundStyle(Color.bronzeIvoryFaint)
                }
                if let names = e.toolCallNames, !names.isEmpty {
                    HStack(spacing: 4) {
                        ForEach(names, id: \.self) { n in
                            Text(n)
                                .font(.caption2.monospaced())
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(Color.bronzeBrass.opacity(0.2))
                                .clipShape(RoundedRectangle(cornerRadius: 4))
                        }
                    }
                }
                if let p = e.preview, !p.isEmpty {
                    Text(p)
                        .font(.callout)
                        .lineLimit(3)
                }
            }
            .padding(.vertical, 4)
        }
        .task { await load() }
        .navigationTitle("Activity")
        .toolbar {
            ToolbarItem {
                Button(action: { Task { await load() } }) {
                    Image(systemName: "arrow.clockwise")
                }
            }
        }
        .bronzeTheme()
    }

    private func load() async {
        do { entries = try await CharlesAPI.shared.activity(limit: 100) }
        catch { self.error = error.localizedDescription }
    }
}

// MARK: - Goals

struct GoalsView: View {
    @State private var goals: [Goal] = []
    @State private var statusFilter = "active"
    @State private var actionInFlight: Set<Int> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Picker("Status", selection: $statusFilter) {
                Text("Active").tag("active")
                Text("All").tag("all")
            }
            .pickerStyle(.segmented)
            .padding()
            .onChange(of: statusFilter) { Task { await load() } }

            List(goals) { g in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("#\(g.id)")
                            .font(.caption.monospacedDigit())
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(statusColor(g.status).opacity(0.2))
                            .clipShape(Capsule())
                        Text(g.status.uppercased())
                            .font(.caption2.bold())
                            .foregroundStyle(statusColor(g.status))
                        Spacer()
                        Text("every \(g.advanceSeconds / 60)m")
                            .font(.caption2)
                            .foregroundStyle(Color.bronzeIvoryDim)
                    }
                    Text(g.description)
                        .lineLimit(3)
                    if !g.notes.isEmpty {
                        Text(g.notes.split(separator: "\n").last.map(String.init) ?? "")
                            .font(.caption.monospaced())
                            .foregroundStyle(Color.bronzeIvoryDim)
                            .lineLimit(2)
                    }
                    if g.status == "active" {
                        Button("Cancel goal") {
                            Task { await cancel(g.id) }
                        }
                        .controlSize(.small)
                        .disabled(actionInFlight.contains(g.id))
                    }
                }
                .padding(.vertical, 6)
            }
        }
        .task { await load() }
        .navigationTitle("Goals")
        .toolbar {
            ToolbarItem {
                Button(action: { Task { await load() } }) {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh goals")
            }
        }
        .bronzeTheme()
    }

    private func statusColor(_ s: String) -> Color {
        switch s {
        case "active":    return .bronzeUser       // active = warm bronze
        case "done":      return .bronzeBrass      // done = success-ish brass
        case "cancelled": return .bronzeIvoryDim   // cancelled = dimmed
        default:          return .bronzeIvoryFaint
        }
    }

    private func load() async {
        do { goals = try await CharlesAPI.shared.goals(status: statusFilter) }
        catch { /* surfaced in main poll error */ }
    }

    private func cancel(_ id: Int) async {
        actionInFlight.insert(id)
        defer { actionInFlight.remove(id) }
        _ = try? await CharlesAPI.shared.cancelGoal(id)
        await load()
    }
}

// MARK: - System

struct SystemView: View {
    @State private var stats: SystemStats?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let s = stats {
                    Group {
                        statBlock(title: "Agent",
                                  value: s.agentRunning ? "Running" : "Stopped",
                                  detail: s.agentRunning ? "pid \(s.agentPid ?? 0) • up \(formatUptime(s.uptimeSeconds))" : "")
                        statBlock(title: "Model",
                                  value: s.model,
                                  detail: "voice: \(s.voice)")
                        if let free = s.ramFreeGb, let active = s.ramActiveGb, let wired = s.ramWiredGb {
                            statBlock(title: "RAM",
                                      value: "\(free) GB free",
                                      detail: "\(active) GB active • \(wired) GB wired")
                        }
                        if let processes = s.charlesProcesses {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Charles processes").font(.headline)
                                ForEach(processes, id: \.self) { p in
                                    Text(p)
                                        .font(.caption.monospaced())
                                        .lineLimit(1)
                                }
                            }
                        }
                    }
                } else {
                    ProgressView()
                }
            }
            .padding()
        }
        .task { await load() }
        .navigationTitle("System")
        .toolbar {
            ToolbarItem {
                Button(action: { Task { await load() } }) {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh system stats")
            }
        }
        .bronzeTheme()
    }

    private func statBlock(title: String, value: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption.bold()).foregroundStyle(Color.bronzeIvoryDim)
            Text(value).font(.title2)
            if !detail.isEmpty {
                Text(detail).font(.caption).foregroundStyle(Color.bronzeIvoryDim)
            }
        }
        .padding()
        .background(Color.bronzeSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func formatUptime(_ seconds: Int?) -> String {
        guard let s = seconds else { return "?" }
        let h = s / 3600, m = (s % 3600) / 60
        return h > 0 ? "\(h)h \(m)m" : "\(m)m"
    }

    private func load() async {
        do { stats = try await CharlesAPI.shared.system() }
        catch { /* ignore */ }
    }
}

// MARK: - Tools

struct ToolsView: View {
    @State private var tools: [ToolEntry] = []

    var body: some View {
        List(tools) { t in
            VStack(alignment: .leading, spacing: 4) {
                Text(t.name).font(.headline.monospaced())
                Text(t.summary)
                    .font(.callout)
                    .foregroundStyle(Color.bronzeIvoryDim)
                if !t.triggers.isEmpty {
                    HStack(spacing: 4) {
                        ForEach(t.triggers, id: \.self) { trig in
                            Text(trig)
                                .font(.caption2)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(Color.bronzeUser.opacity(0.15))
                                .clipShape(Capsule())
                        }
                    }
                }
            }
            .padding(.vertical, 4)
        }
        .task { await load() }
        .navigationTitle("Tools")
        .toolbar {
            ToolbarItem {
                Button(action: { Task { await load() } }) {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh tools")
            }
        }
        .bronzeTheme()
    }

    private func load() async {
        do { tools = try await CharlesAPI.shared.tools() } catch { /* ignore */ }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var config: CharlesConfig
    @State private var url: String = ""
    @State private var secret: String = ""
    @State private var saved = false
    @State private var loadStatus: String = ""

    // Local-server case: read the secret directly from the Mac filesystem so
    // the user never has to fight macOS's Strong Password autofill on the field.
    private let secretFilePath = "/Users/home/charles/workspace/warroom_secret.txt"

    var body: some View {
        Form {
            Section("Charles Server") {
                TextField("Server URL", text: $url)
                    .textFieldStyle(.roundedBorder)
                // Plain TextField (not SecureField) — SecureField triggers macOS's
                // Strong Password autofill which intercepts typing and can block input
                // entirely. For a single-user personal tool the secret being visible
                // on screen is an acceptable trade for being able to actually paste/type it.
                TextField("Shared secret", text: $secret)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.body, design: .monospaced))
                HStack(spacing: 8) {
                    Button("Load from secret file…") {
                        let panel = NSOpenPanel()
                        panel.title = "Pick warroom_secret.txt"
                        panel.allowedContentTypes = [.plainText, .data]
                        panel.canChooseDirectories = false
                        panel.allowsMultipleSelection = false
                        panel.directoryURL = URL(fileURLWithPath: "/Users/home/charles/workspace/")
                        if panel.runModal() == .OK, let url = panel.url,
                           let s = try? String(contentsOf: url, encoding: .utf8) {
                            secret = s.trimmingCharacters(in: .whitespacesAndNewlines)
                            loadStatus = "Loaded \(s.trimmingCharacters(in: .whitespacesAndNewlines).count) chars from \(url.lastPathComponent)."
                        }
                    }
                    .help("Opens a file picker — point at workspace/warroom_secret.txt. Sandbox-safe (uses user-selected file entitlement).")
                    Button("Paste from clipboard") {
                        if let pasted = NSPasteboard.general.string(forType: .string) {
                            secret = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
                            loadStatus = "Pasted \(secret.count) chars."
                        }
                    }
                    .help("Reads the secret from your clipboard. Useful if you copied it via 'pbcopy < ~/charles/workspace/warroom_secret.txt' in Terminal.")
                    Spacer()
                }
                if !loadStatus.isEmpty {
                    Text(loadStatus).font(.caption).foregroundStyle(Color.bronzeIvoryDim)
                }
                Text("Get the secret from `\(secretFilePath)` on the Mac Studio. Server URL is `http://<tailscale-ip>:8765` once Tailscale is up.")
                    .font(.caption)
                    .foregroundStyle(Color.bronzeIvoryDim)
            }
            HStack {
                Button("Save") {
                    config.serverURL = URL(string: url) ?? config.serverURL
                    config.sharedSecret = secret
                    config.save()
                    saved = true
                }
                if saved {
                    Text("Saved.").foregroundStyle(Color.bronzeBrass).font(.caption)
                }
            }
        }
        .padding()
        .frame(width: 480)
        .onAppear {
            url = config.serverURL.absoluteString
            secret = config.sharedSecret
        }
        .bronzeTheme()
    }
}

// MARK: - Secrets (in-app credentials channel — replaces "paste in chat" anti-pattern)

struct SecretsView: View {
    @State private var secrets: [CharlesAPI.SecretEntry] = []
    @State private var loading = false
    @State private var error: String?

    @State private var newName: String = ""
    @State private var newValue: String = ""
    @State private var showValue: Bool = false
    @State private var saving = false
    @State private var saveStatus: String = ""
    @State private var pendingRestart = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    addForm
                    Divider()
                    listSection
                    explainerFooter
                }
                .padding()
            }
        }
        .navigationTitle("Secrets")
        .toolbar {
            ToolbarItem {
                Button(action: { Task { await load() } }) {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh secrets list")
            }
        }
        .bronzeTheme()
        .task { await load() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "key.fill").foregroundStyle(.tint)
            Text("Credentials → ~/charles/.env").font(.headline)
            Spacer()
            if pendingRestart {
                Button("Restart Charles to apply") {
                    Task { await restartCharles() }
                }
                .buttonStyle(.borderedProminent)
                .tint(Color.bronzeCopper)
            }
        }
        .padding()
    }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Add or update").font(.headline)
            HStack(spacing: 8) {
                TextField("KEY_NAME (e.g. STRIPE_SECRET_KEY)", text: $newName)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.body, design: .monospaced))
                    .onChange(of: newName) { _, n in
                        // Auto-uppercase + replace spaces with underscores for convenience
                        let cleaned = n.uppercased().replacingOccurrences(of: " ", with: "_")
                        if cleaned != n { newName = cleaned }
                    }
            }
            HStack(spacing: 8) {
                Group {
                    if showValue {
                        TextField("Value (paste here)", text: $newValue)
                    } else {
                        SecureField("Value (paste here — hidden)", text: $newValue)
                    }
                }
                .textFieldStyle(.roundedBorder)
                .font(.system(.body, design: .monospaced))
                Button(action: { showValue.toggle() }) {
                    Image(systemName: showValue ? "eye.slash" : "eye")
                }
                .help("Toggle value visibility")
                Button("Paste") {
                    if let s = NSPasteboard.general.string(forType: .string) {
                        newValue = s.trimmingCharacters(in: .whitespacesAndNewlines)
                    }
                }
                .help("Paste from clipboard")
            }
            HStack {
                Button(saving ? "Saving…" : "Save to .env") {
                    Task { await save() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(saving || newName.isEmpty || newValue.isEmpty)
                Spacer()
                if !saveStatus.isEmpty {
                    Text(saveStatus).font(.caption).foregroundStyle(Color.bronzeIvoryDim)
                }
            }
        }
    }

    private var listSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Currently in .env").font(.headline)
            if let e = error {
                Text(e).foregroundStyle(Color.bronzeError).font(.caption)
            }
            if secrets.isEmpty && !loading {
                Text("(none yet — add your first secret above)")
                    .foregroundStyle(Color.bronzeIvoryDim)
                    .italic()
            } else {
                ForEach(secrets) { s in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s.name).font(.system(.body, design: .monospaced).weight(.bold))
                            Text("\(s.preview)  ·  \(s.length) chars")
                                .font(.caption.monospaced())
                                .foregroundStyle(Color.bronzeIvoryDim)
                        }
                        Spacer()
                        Button(role: .destructive) {
                            Task { await delete(s.name) }
                        } label: {
                            Image(systemName: "trash")
                        }
                        .help("Delete this secret from .env")
                    }
                    .padding(8)
                    .background(Color.bronzeSurface)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
            }
        }
    }

    private var explainerFooter: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Why this exists").font(.caption.bold()).foregroundStyle(Color.bronzeIvoryDim)
            Text("• Pasting a credential in chat puts it in conversation history, daily logs, and (worst case) gets remembered as a long-term fact. This channel writes straight to ~/charles/.env (gitignored, mode 0600) — the only safe pipe.")
            Text("• Charles needs a restart to pick up the new value (env vars are loaded at process boot). Click 'Restart Charles' above after saving.")
            Text("• Reachable from your iPhone the same way the rest of the app is — over Tailscale.")
        }
        .font(.caption)
        .foregroundStyle(Color.bronzeIvoryDim)
    }

    private func load() async {
        loading = true; defer { loading = false }
        do {
            secrets = try await CharlesAPI.shared.secretsList()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save() async {
        saving = true; defer { saving = false }
        do {
            let r = try await CharlesAPI.shared.secretSet(name: newName, value: newValue)
            saveStatus = r.is_new ? "Added \(r.name)." : "Updated \(r.name)."
            pendingRestart = pendingRestart || r.restart_needed
            newName = ""; newValue = ""
            await load()
        } catch {
            saveStatus = "Failed: \(error.localizedDescription)"
        }
    }

    private func delete(_ name: String) async {
        do {
            let r = try await CharlesAPI.shared.secretDelete(name: name)
            pendingRestart = pendingRestart || r.restart_needed
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func restartCharles() async {
        do {
            _ = try await CharlesAPI.shared.restartCharles()
            pendingRestart = false
            saveStatus = "Charles restarted. New env values are live."
        } catch {
            saveStatus = "Restart failed: \(error.localizedDescription)"
        }
    }
}
