import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// ScheduleView — v1.0.4 P2-2. Manage scheduled download tasks
/// (URL + fire-at time). At the scheduled time the engine enqueues
/// the URL via the same path as `add(url:)`. The picker is
/// minimal: add URL + DatePicker + optional note.
struct ScheduleView: View {
    @ObservedObject var store: DownloadListStore
    @Environment(\.strings) private var S
    @State private var newURL: String = ""
    @State private var newDate: Date = Date().addingTimeInterval(60)
    @State private var newNote: String = ""
    @State private var error: String? = nil
    @State private var schedules: [ScheduledItem] = []
    @State private var timerTask: Task<Void, Never>? = nil

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("https://example.com/file.zip", text: $newURL)
                    .textFieldStyle(.roundedBorder)
                DatePicker("", selection: $newDate,
                           in: Date()...,
                           displayedComponents: [.date, .hourAndMinute])
                    .labelsHidden()
                Button(S.scheduleAdd) { commit() }
                    .disabled(newURL.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(12)
            .background(.bar)
            Divider()
            if let err = error {
                Text(err).font(.caption).foregroundStyle(.red).padding(8)
            }
            if schedules.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "alarm")
                        .font(.system(size: 36))
                        .foregroundStyle(.tertiary)
                    Text(S.scheduleEmpty)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Text(S.scheduleHint)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    ForEach(schedules) { item in
                        HStack {
                            Image(systemName: "alarm.fill")
                                .foregroundStyle(.tint)
                            VStack(alignment: .leading) {
                                Text(item.url.absoluteString)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                Text(item.fireAt, format: .dateTime.month().day().hour().minute())
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button {
                                schedules.removeAll { $0.id == item.id }
                            } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.borderless)
                        }
                    }
                }
                .listStyle(.inset)
            }
        }
        .onAppear { startTimer() }
    }

    private func commit() {
        let trimmed = newURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else {
            error = "Enter a valid http(s) URL."
            return
        }
        let item = ScheduledItem(
            id: UUID(), url: url, fireAt: newDate, note: newNote)
        schedules.append(item)
        newURL = ""
        newNote = ""
        error = nil
    }

    private func startTimer() {
        timerTask?.cancel()
        timerTask = Task { [self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                let now = Date()
                let due = schedules.filter { $0.fireAt <= now }
                if !due.isEmpty {
                    for d in due {
                        store.add(url: d.url)
                    }
                    schedules.removeAll { d in due.contains(where: { $0.id == d.id }) }
                }
            }
        }
    }
}

struct ScheduledItem: Identifiable, Equatable {
    let id: UUID
    let url: URL
    let fireAt: Date
    let note: String
}
