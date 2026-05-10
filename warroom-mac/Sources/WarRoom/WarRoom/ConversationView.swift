// ConversationView.swift — chat pane. Pick a conversation from the list,
// see turns, type a reply.

import SwiftUI
import AppKit

// MARK: - Smart text editor — Enter sends, Shift+Enter inserts newline

struct ChatComposerField: NSViewRepresentable {
    @Binding var text: String
    var onSubmit: () -> Void
    var disabled: Bool = false

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = true

        let textView = SubmittingTextView()
        textView.delegate = context.coordinator
        textView.coordinator = context.coordinator
        textView.isRichText = false
        textView.isEditable = !disabled
        textView.font = NSFont.systemFont(ofSize: 14)
        textView.textContainerInset = NSSize(width: 6, height: 6)
        textView.autoresizingMask = [.width]
        textView.allowsUndo = true
        scroll.documentView = textView
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = scroll.documentView as? SubmittingTextView else { return }
        if tv.string != text { tv.string = text }
        tv.isEditable = !disabled
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(text: $text, onSubmit: onSubmit)
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        let onSubmit: () -> Void

        init(text: Binding<String>, onSubmit: @escaping () -> Void) {
            self.text = text
            self.onSubmit = onSubmit
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = notification.object as? NSTextView else { return }
            text.wrappedValue = tv.string
        }
    }
}

final class SubmittingTextView: NSTextView {
    weak var coordinator: ChatComposerField.Coordinator?

    override func keyDown(with event: NSEvent) {
        // Enter alone = submit; Shift+Enter = newline (default)
        if event.keyCode == 36 /* Return */ && !event.modifierFlags.contains(.shift) {
            coordinator?.onSubmit()
            return
        }
        super.keyDown(with: event)
    }
}

struct ConversationView: View {
    @State private var conversations: [ConversationIndexEntry] = []
    @State private var selectedConvId: String?
    @State private var turns: [ConversationTurn] = []
    @State private var draft = ""
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        HSplitView {
            // Left: list of conversations
            VStack(alignment: .leading, spacing: 0) {
                Text("CONVERSATIONS")
                    .font(.caption.bold())
                    .tracking(1.5)
                    .foregroundStyle(Color.bronzeCopper)
                    .padding()
                List(conversations, selection: $selectedConvId) { c in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(c.conversationId)
                            .font(.system(.body, design: .monospaced))
                            .foregroundStyle(Color.bronzeIvory)
                            .lineLimit(1)
                        if let last = c.lastUserMsg {
                            Text(last)
                                .font(.caption)
                                .foregroundStyle(Color.bronzeIvoryDim)
                                .lineLimit(2)
                        }
                        Text("\(c.turnCount) turns • \(c.lastAt.prefix(19))")
                            .font(.caption2)
                            .foregroundStyle(Color.bronzeIvoryFaint)
                    }
                    .padding(.vertical, 4)
                    .tag(c.conversationId)
                }
                .listStyle(.sidebar)
                .scrollContentBackground(.hidden)
                .background(Color.bronzeSurface)
            }
            .frame(minWidth: 250, idealWidth: 280, maxWidth: 350)
            .background(Color.bronzeSurface)

            // Right: turns + composer
            VStack(spacing: 0) {
                if let convId = selectedConvId {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 12) {
                                ForEach(turns) { turn in
                                    TurnRow(turn: turn).id(turn.id)
                                }
                                // Bottom anchor we can scroll to
                                Color.clear.frame(height: 1).id("bottom")
                            }
                            .padding()
                        }
                        .onChange(of: turns.count) { _, _ in
                            // Auto-scroll to bottom when new turns arrive
                            withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                        }
                        .onAppear {
                            proxy.scrollTo("bottom", anchor: .bottom)
                        }
                    }
                    Divider()
                    composer(convId: convId)
                } else {
                    VStack(spacing: 12) {
                        Image(systemName: "gearshape.2.fill")
                            .font(.system(size: 48))
                            .foregroundStyle(Color.bronzeDeep)
                        Text("Pick a conversation.")
                            .font(.callout)
                            .foregroundStyle(Color.bronzeIvoryFaint)
                            .tracking(0.5)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.bronzeBackground)
                }
            }
        }
        .task { await loadConversations() }
        .onChange(of: selectedConvId) { _, new in
            if let id = new { Task { await loadTurns(id) } }
        }
        // Auto-poll the selected conversation every 2 seconds so Charles's
        // replies + progress ticker show up without a manual refresh.
        // Restarts each time selectedConvId changes (the .task(id:) idiom).
        .task(id: selectedConvId) {
            guard let id = selectedConvId else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)  // 2s
                if Task.isCancelled { break }
                await loadTurns(id)
            }
        }
        .navigationTitle("Conversation")
        .toolbar {
            ToolbarItem {
                Button(action: {
                    Task {
                        await loadConversations()
                        if let id = selectedConvId { await loadTurns(id) }
                    }
                }) {
                    Image(systemName: "arrow.clockwise")
                        .foregroundStyle(Color.bronzeCopper)
                }
                .help("Refresh conversations + turns")
            }
        }
        .bronzeTheme()
    }

    private func composer(convId: String) -> some View {
        VStack(spacing: 6) {
            ChatComposerField(
                text: $draft,
                onSubmit: { Task { await send(convId: convId) } },
                disabled: sending
            )
            .frame(minHeight: 48, maxHeight: 140)

            HStack(spacing: 8) {
                // Kickstart — wipe rolling context if Charles is loop-stuck
                Button(action: { Task { await kickstart(convId: convId) } }) {
                    Label("Kickstart", systemImage: "arrow.counterclockwise.circle")
                }
                .help("Wipe Charles's recent rolling context for this conversation. Use when he's pattern-stuck. Long-term facts + goals untouched.")

                Spacer()

                if sending {
                    ProgressView()
                        .scaleEffect(0.5)
                        .tint(Color.bronzeCopper)
                    Text("Charles is workin'…")
                        .font(.caption2.italic())
                        .foregroundStyle(Color.bronzeProgress)
                }

                // Stop — cancel the in-flight response
                Button(role: .destructive, action: { Task { await stop(convId: convId) } }) {
                    Label("Stop", systemImage: "stop.circle.fill")
                        .labelStyle(.titleAndIcon)
                }
                .tint(.red)
                .disabled(!sending)
                .help("Stop Charles mid-response. Useful if you sent the wrong message or he's spiraling. He'll exit at the next round checkpoint (5-30 sec).")

                // Send
                Button(action: { Task { await send(convId: convId) } }) {
                    Image(systemName: "paperplane.fill")
                        .font(.title2)
                }
                .keyboardShortcut(.return, modifiers: [])
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty || sending)
            }
            if let e = error {
                HStack {
                    Text(e).font(.caption).foregroundStyle(Color.bronzeError)
                    Spacer()
                    Button("Dismiss") { self.error = nil }.buttonStyle(.borderless).font(.caption)
                }
            }
        }
        .padding()
    }

    private func stop(convId: String) async {
        do {
            let r = try await CharlesAPI.shared.stopConversation(convId: convId)
            error = r.found_in_flight
                ? "Stop signal sent — Charles will exit at next round."
                : "Nothing in flight to stop."
        } catch {
            self.error = "Stop failed: \(error.localizedDescription)"
        }
    }

    private func kickstart(convId: String) async {
        do {
            let r = try await CharlesAPI.shared.resetConversation(convId: convId)
            error = "Kickstart: cleared \(r.deleted) turns. Send a fresh message."
            await loadTurns(convId)
        } catch {
            self.error = "Kickstart failed: \(error.localizedDescription)"
        }
    }

    private func loadConversations() async {
        do {
            conversations = try await CharlesAPI.shared.conversationsIndex()
            if selectedConvId == nil, let first = conversations.first {
                selectedConvId = first.conversationId
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadTurns(_ convId: String) async {
        do {
            turns = try await CharlesAPI.shared.conversation(convId, limit: 100)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func send(convId: String) async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        // Optimistic UI: append the user's message immediately so they see it,
        // even before the server round-trip completes. Real turn list will
        // replace it when loadTurns runs.
        let provisional = ConversationTurn(
            id: -Int.random(in: 1...1_000_000),
            role: "user",
            content: text,
            toolCallId: nil,
            createdAt: ISO8601DateFormatter().string(from: Date())
        )
        turns.append(provisional)
        let originalDraft = draft
        draft = ""
        sending = true
        defer { sending = false }
        do {
            _ = try await CharlesAPI.shared.sendMessage(convId: convId, text: text)
            await loadTurns(convId)
        } catch {
            // Restore draft on failure so the user can retry without retyping
            self.error = "Send failed: \(error.localizedDescription)"
            draft = originalDraft
            // Remove the provisional turn so we don't show a stale message
            turns.removeAll { $0.id == provisional.id }
        }
    }
}

struct TurnRow: View {
    let turn: ConversationTurn

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            roleIcon
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(turn.role.uppercased())
                        .font(.caption2.bold())
                        .foregroundStyle(roleColor)
                        .tracking(1.0)  // letter-spacing for industrial feel
                    Text(turn.createdAt.prefix(19))
                        .font(.caption2)
                        .foregroundStyle(Color.bronzeIvoryFaint)
                    Spacer()
                }
                if let c = turn.content, !c.isEmpty {
                    if turn.role == "progress" {
                        // Single-row mutating ticker — italic, dim, faint
                        Text(stripBracketingItalics(c))
                            .italic()
                            .font(.system(.callout, design: .monospaced))
                            .foregroundStyle(Color.bronzeProgress)
                    } else {
                        Text(c)
                            .foregroundStyle(Color.bronzeIvory)
                            .textSelection(.enabled)
                    }
                }
            }
        }
    }

    /// Strip `*…*` markdown italics — we apply italic styling natively.
    private func stripBracketingItalics(_ s: String) -> String {
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasPrefix("*") && trimmed.hasSuffix("*") && trimmed.count > 2 {
            return String(trimmed.dropFirst().dropLast())
        }
        return s
    }

    private var roleColor: Color {
        switch turn.role {
        case "user":      return .bronzeUser
        case "assistant": return .bronzeAssistant
        case "tool":      return .bronzeTool
        case "progress":  return .bronzeProgress
        case "system":    return .bronzeIvoryDim
        default:          return .bronzeIvoryFaint
        }
    }

    private var roleIcon: some View {
        Image(systemName: roleIconName)
            .foregroundStyle(roleColor)
    }

    private var roleIconName: String {
        switch turn.role {
        case "user":      return "person.fill"
        case "assistant": return "gearshape.2.fill"   // mechanical, matches the icon
        case "tool":      return "wrench.adjustable"
        case "progress":  return "ellipsis.circle"    // mid-action ticker
        case "system":    return "gear"
        default:          return "circle"
        }
    }
}
