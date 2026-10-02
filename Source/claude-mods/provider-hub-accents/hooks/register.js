// Presentation only: no request, tool, permission, prompt or routing hooks.
// Resolve the model on every render, so changing /model or /resume cannot
// retain another conversation's colour. No shared store or "last model" cache.
export function register(on, options) {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)

    let brand
    try {
      // The env form also supports a disposable --plugin-dir preview.
      const cataloguePath = options.cataloguePath || await $.env.get('PROVIDER_HUB_CLAUDE_ACCENTS')
      if (!cataloguePath) return next(e)
      const gateway = await $.env.get('ANTHROPIC_BASE_URL')
      // A local Hub profile is required even when a native Claude model id
      // happens to occur in the catalogue's compatibility slots.
      if (typeof gateway !== 'string' || !/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(gateway)) return next(e)
      const catalogue = JSON.parse(await $.fs.read(cataloguePath))
      if (catalogue.schema !== 1 || catalogue.active !== true ||
          gateway.replace(/\/$/, '') !== catalogue.gatewayUrl) return next(e)
      const model = await $.session.model()
      // Claude's context suffix is not part of a route's identity.
      const id = model.replace(/\[1m\]$/i, '')
      if (!catalogue.models || !Object.hasOwn(catalogue.models, id)) return next(e)
      const entry = catalogue.models[id]
      if (!entry || !/^#[0-9a-f]{6}$/i.test(entry.accent) ||
          typeof entry.modelLabel !== 'string' || !entry.modelLabel.trim() ||
          entry.modelLabel.length > 160 || /[\x00-\x1f\x7f]/.test(entry.modelLabel)) return next(e)
      brand = entry
    } catch {
      // Missing, stale or unreadable presentation data never prevents drawing.
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const native = await next(e)
    // AbovePrompt is shared with other mods. Keep their tree exactly once.
    return Box({
      flexDirection: 'column',
      children: [
        Text({
          wrap: 'truncate',
          children: [
            Text({ color: brand.accent, children: ['● '] }),
            Text({ dimColor: true, children: [brand.modelLabel] }),
          ],
        }),
        native,
      ],
    })
  })
}
