// ApprovalsView.swift — the unified Tasks tab.
//
// Aggregates 3 sources Charles surfaces things from:
//   - approvals: Tier-2 governance gates (request_approval tool)
//   - tasks: Charles-created or auto-extracted from his replies (add_task tool)
//   - open_requests: time-tracked follow-ups (track_open_request tool)
//
// Plus an "Add task" form so John can drop his own todo here too.

import SwiftUI

struct ApprovalsView: View {
    @State private var items: [CharlesAPI.UnifiedTask] = []
    @State private var loading = true
    @State private var error: String?
    @State private var inFlight: Set<String> = []
    @State private var showAddForm = false
    @State private var newTitle = ""
    @State private var newDescription = ""
    @State private var newUrgency = "normal"

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if showAddForm { addForm }
            if loading && items.isEmpty {
                ProgressView("Loading…").padding()
            } else if let e = error {
                Text("Error: \(e)").foregroundStyle(Color.bronzeError).padding()
            } else if items.isEmpty {
                emptyState
            } else {
                List(items) { t in
                    TaskRow(
                        task: t,
                        inFlight: inFlight.contains(t.id),
                        onAct: { approve in Task { await act(t, approve: approve) } }
                    )
                }
                .listStyle(.inset)
            }
        }
        .task { await load() }
        .navigationTitle("Tasks")
        .toolbar {
            ToolbarItem {
                Button(action: { withAnimation { showAddForm.toggle() } }) {
                    Image(systemName: showAddForm ? "minus.circle" : "plus.circle")
                }
                .help(showAddForm ? "Hide add-task form" : "Add a task")
            }
            ToolbarItem {
                Button(action: { Task { await load() } }) {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
            }
        }
        .bronzeTheme()
    }

    private var header: some View {
        HStack {
            Image(systemName: items.isEmpty ? "checkmark.shield.fill" : "exclamationmark.shield.fill")
                .font(.title2)
                .foregroundStyle(items.isEmpty ? Color.bronzeBrass : Color.bronzeCopper)
            VStack(alignment: .leading) {
                Text("Tasks Charles needs from you")
                    .font(.title2.bold())
                Text(items.isEmpty
                     ? "Nothing waiting on you."
                     : "\(items.count) item\(items.count == 1 ? "" : "s") — \(blockingCount) blocking, \(highCount) high.")
                    .foregroundStyle(Color.bronzeIvoryDim)
                    .font(.subheadline)
            }
            Spacer()
        }
        .padding()
    }

    private var blockingCount: Int { items.filter { $0.urgency == "blocking" }.count }
    private var highCount: Int { items.filter { $0.urgency == "high" }.count }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Add a task").font(.headline)
            TextField("Title (e.g. 'Buy SSL cert for promptaiengineering.com')", text: $newTitle)
                .textFieldStyle(.roundedBorder)
            TextField("Description (optional)", text: $newDescription)
                .textFieldStyle(.roundedBorder)
            HStack {
                Picker("Urgency", selection: $newUrgency) {
                    Text("Low").tag("low")
                    Text("Normal").tag("normal")
                    Text("High").tag("high")
                    Text("Blocking").tag("blocking")
                }
                .pickerStyle(.segmented)
                Button("Add") {
                    Task { await addTask() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(newTitle.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding()
        .background(Color.bronzeSurface)
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.shield.fill")
                .font(.system(size: 48))
                .foregroundStyle(Color.bronzeBrass)
            Text("All clear.")
                .font(.title3)
            Text("Charles isn't blocked on anything. He'll appear here when he needs your call.")
                .foregroundStyle(Color.bronzeIvoryDim)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 400)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }

    private func load() async {
        loading = true
        error = nil
        do {
            items = try await CharlesAPI.shared.tasksUnified()
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    private func act(_ t: CharlesAPI.UnifiedTask, approve: Bool) async {
        inFlight.insert(t.id)
        defer { inFlight.remove(t.id) }
        do {
            switch t.kind {
            case "approval":
                if approve {
                    _ = try await CharlesAPI.shared.approve(factId: t.raw_id)
                } else {
                    _ = try await CharlesAPI.shared.deny(factId: t.raw_id, reason: "denied via War Room")
                }
            case "task":
                if approve {
                    _ = try await CharlesAPI.shared.taskComplete(id: t.raw_id)
                } else {
                    _ = try await CharlesAPI.shared.taskDismiss(id: t.raw_id)
                }
            case "open_request":
                // Treating "approve" as resolve, "deny" as dismiss; they both clear from the list.
                // Backend doesn't have a clean "dismiss open_request" yet; we just refresh.
                break
            default:
                break
            }
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func addTask() async {
        do {
            _ = try await CharlesAPI.shared.taskAdd(
                title: newTitle.trimmingCharacters(in: .whitespaces),
                description: newDescription.trimmingCharacters(in: .whitespaces),
                urgency: newUrgency
            )
            newTitle = ""
            newDescription = ""
            newUrgency = "normal"
            await load()
        } catch {
            self.error = "Add task failed: \(error.localizedDescription)"
        }
    }
}

struct TaskRow: View {
    let task: CharlesAPI.UnifiedTask
    let inFlight: Bool
    let onAct: (Bool) -> Void   // true = approve/done, false = deny/dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                kindBadge
                urgencyBadge
                if let src = task.source, !src.isEmpty {
                    Text(src.uppercased())
                        .font(.caption2.monospaced())
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Color.bronzeIvoryFaint.opacity(0.15))
                        .clipShape(Capsule())
                }
                Spacer()
                Text(relativeTime(task.created_at))
                    .font(.caption)
                    .foregroundStyle(Color.bronzeIvoryDim)
            }
            Text(task.title)
                .font(.body.weight(.medium))
                .textSelection(.enabled)
            if let d = task.description, !d.isEmpty, d != task.title {
                Text(d)
                    .font(.callout)
                    .foregroundStyle(Color.bronzeIvoryDim)
                    .lineLimit(3)
                    .textSelection(.enabled)
            }
            if let conv = task.source_conv {
                Text("from conv: \(conv)")
                    .font(.caption.monospaced())
                    .foregroundStyle(Color.bronzeIvoryFaint)
            }
            actionButtons
        }
        .padding(.vertical, 6)
    }

    private var kindBadge: some View {
        let label: String
        let color: Color
        switch task.kind {
        case "approval":     label = "APPROVAL"; color = .red
        case "task":         label = "TASK"; color = .blue
        case "open_request": label = "WAITING"; color = .orange
        default:             label = task.kind.uppercased(); color = .gray
        }
        return Text(label)
            .font(.caption2.bold())
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(color.opacity(0.18))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }

    private var urgencyBadge: some View {
        let color: Color
        switch task.urgency {
        case "blocking": color = .red
        case "high":     color = .orange
        case "low":      color = .gray
        default:         color = .blue
        }
        return Text(task.urgency.uppercased())
            .font(.caption2.bold().monospaced())
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(color.opacity(0.15))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }

    @ViewBuilder
    private var actionButtons: some View {
        switch task.kind {
        case "approval":
            HStack(spacing: 12) {
                Button(action: { onAct(false) }) {
                    Label("Deny", systemImage: "xmark.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(Color.bronzeError).disabled(inFlight)

                Button(action: { onAct(true) }) {
                    Label("Approve", systemImage: "checkmark.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(Color.bronzeBrass).disabled(inFlight)
            }
            .controlSize(.large)
        case "task":
            HStack(spacing: 12) {
                Button(action: { onAct(false) }) {
                    Label("Dismiss", systemImage: "trash")
                }
                .buttonStyle(.bordered).disabled(inFlight)

                Button(action: { onAct(true) }) {
                    Label("Done", systemImage: "checkmark.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(Color.bronzeBrass).disabled(inFlight)
            }
            .controlSize(.regular)
        case "open_request":
            HStack {
                Spacer()
                Text("Charles is waiting on this — reply to him in chat to clear")
                    .font(.caption)
                    .foregroundStyle(Color.bronzeIvoryDim)
            }
        default:
            EmptyView()
        }
    }

    private func relativeTime(_ iso: String) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let d = f.date(from: iso) else { return iso }
        let r = RelativeDateTimeFormatter()
        return r.localizedString(for: d, relativeTo: Date())
    }
}
