import SwiftUI

/// ExpiredLinkBanner — P2 stub view used by P3's `ExpiredLinkHandler`
/// when the engine reports a 403/410/451 mid-flight. The banner is
/// non-blocking by design (no modal that freezes the rest of the UI).
///
/// v1.0 layout:
/// - one row at the top of the main window
/// - "刷新链接" button opens a sheet where the user pastes the new
///   URL. The sheet itself lives in P3 and calls
///   `ExpiredLinkHandler.evaluateRelink`; the banner only knows
///   about state transitions.
struct ExpiredLinkBanner: View {
    let summary: String
    let onReenter: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "link.badge.plus")
                .foregroundStyle(.orange)
            Text(summary)
            Spacer()
            Button("刷新链接", action: onReenter)
                .buttonStyle(.borderedProminent)
        }
        .padding(12)
        .background(.regularMaterial)
        .cornerRadius(8)
        .padding(.horizontal)
    }
}
