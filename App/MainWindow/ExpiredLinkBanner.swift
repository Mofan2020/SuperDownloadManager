import SwiftUI
import SuperDownloadManagerCore

/// ExpiredLinkBanner — non-blocking banner shown when the engine
/// reports a 403/410/451 mid-flight.
struct ExpiredLinkBanner: View {
    let summary: String
    let onReenter: () -> Void
    @Environment(\.strings) private var S

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "link.badge.plus")
                .foregroundStyle(.orange)
            Text(summary)
            Spacer()
            Button(S.expiredRefresh, action: onReenter)
                .buttonStyle(.borderedProminent)
        }
        .padding(12)
        .background(.regularMaterial)
        .cornerRadius(8)
        .padding(.horizontal)
    }
}
