import SwiftUI

import SuperDownloadManagerCore

/// ExpiredLinkBanner — non-blocking banner shown when the engine
/// reports a 403/410/451 mid-flight. v1.0.1 added bilingual text
/// support so the "Refresh link" button label is localizable.
struct ExpiredLinkBanner: View {
    let summary: String
    let onReenter: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "link.badge.plus")
                .foregroundStyle(.orange)
            Text(summary)
            Spacer()
            Button(L10n.bannerRefreshLink, action: onReenter)
                .buttonStyle(.borderedProminent)
        }
        .padding(12)
        .background(.regularMaterial)
        .cornerRadius(8)
        .padding(.horizontal)
    }
}
