import { expect, mock, test } from 'claude-code/testing'

const band = {
  plugin: 'provider-hub-accents',
  component: 'AbovePrompt', requestId: 'above-prompt',
  viewport: { columns: 100, rows: 30 },
  props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 80, view: {} },
}
const data = {
  schema: 1, active: true, gatewayUrl: 'http://127.0.0.1:11436',
  models: {
    'claude-fable-5-example-one': { accent: '#123ABC', modelLabel: 'Model One' },
    'claude-fable-5-example-two': { accent: '#987654', modelLabel: 'Model Two' },
  },
}

test('draws on both surfaces and preserves the other mod', async ($, on) => {
  mock.env(on, { ANTHROPIC_BASE_URL: data.gatewayUrl, PROVIDER_HUB_CLAUDE_ACCENTS: '/test/claude-accents.json' })
  on('fs.read', () => ({ value: JSON.stringify(data) }))
  on('session.model', () => ({ value: 'claude-fable-5-example-one[1m]' }))
  let calls = 0
  on('ui.render', () => {
    calls += 1
    return { type: 'Text', props: {}, children: ['other mod'] }
  })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...band, surface })
    expect((await ui.find({ type: 'Text', text: '● Model One' }))?.children[0].props.color).toBe('#123ABC')
    expect(await ui.find({ type: 'Text', text: 'other mod' })).toBeDefined()
    await ui.unmount()
  }
  expect(calls).toBe(2)
})

test('model switches and unknown models never retain the previous accent', async ($, on) => {
  mock.env(on, { ANTHROPIC_BASE_URL: data.gatewayUrl, PROVIDER_HUB_CLAUDE_ACCENTS: '/test/claude-accents.json' })
  on('fs.read', () => ({ value: JSON.stringify(data) }))
  let model = 'claude-fable-5-example-one'
  on('session.model', () => ({ value: model }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['native'] }))
  for (const [id, label, accent] of [
    [model, 'Model One', '#123ABC'],
    ['claude-fable-5-example-two', 'Model Two', '#987654'],
    ['unknown', undefined, undefined],
    ['claude-fable-5-example-one', 'Model One', '#123ABC'],
  ]) {
    model = id
    const ui = await $.ui.mount({ ...band, surface: 'desktop' })
    expect((await ui.find({ type: 'Text', text: /^● / }))?.children[0].props.color).toBe(accent)
    if (label) expect(await ui.find({ type: 'Text', text: '● ' + label })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'native' })).toBeDefined()
    await ui.unmount()
  }
})

for (const gateway of [undefined, 'https://api.anthropic.com', 'http://127.0.0.1:9999']) {
  test('does nothing outside its Hub gateway: ' + gateway, async ($, on) => {
    mock.env(on, { ANTHROPIC_BASE_URL: gateway, PROVIDER_HUB_CLAUDE_ACCENTS: '/test/claude-accents.json' })
    on('fs.read', () => ({ value: JSON.stringify(data) }))
    on('session.model', () => ({ value: 'claude-fable-5-example-one' }))
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['native'] }))
    const ui = await $.ui.mount({ ...band, surface: 'desktop' })
    expect(await ui.find({ type: 'Text', text: /^● / })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'native' })).toBeDefined()
    await ui.unmount()
  })
}

for (const value of ['broken JSON', JSON.stringify({ ...data, active: false }), JSON.stringify({ ...data, schema: 9 })]) {
  test('ignores unusable catalogues: ' + value.slice(0, 55), async ($, on) => {
    mock.env(on, { ANTHROPIC_BASE_URL: data.gatewayUrl, PROVIDER_HUB_CLAUDE_ACCENTS: '/test/claude-accents.json' })
    on('fs.read', () => ({ value }))
    on('session.model', () => ({ value: 'claude-fable-5-example-one' }))
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['native'] }))
    const ui = await $.ui.mount({ ...band, surface: 'desktop' })
    expect(await ui.find({ type: 'Text', text: /^● / })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'native' })).toBeDefined()
    await ui.unmount()
  })
}

test('a survey keeps the whole band', async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['survey'] }))
  const ui = await $.ui.mount({ ...band, surface: 'desktop', props: { ...band.props, hasSurvey: true } })
  expect(await ui.find({ type: 'Text', text: 'survey' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^● / })).toBeUndefined()
  await ui.unmount()
})

test('a missing file does not block the native interface', async ($, on) => {
  mock.env(on, { ANTHROPIC_BASE_URL: data.gatewayUrl, PROVIDER_HUB_CLAUDE_ACCENTS: '/test/missing.json' })
  on('fs.read', () => ({ deny: 'file missing' }))
  let nativeCalls = 0
  on('ui.render', () => {
    nativeCalls += 1
    return { type: 'Text', props: {}, children: ['native'] }
  })
  const ui = await $.ui.mount({ ...band, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: 'native' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^● / })).toBeUndefined()
  expect(nativeCalls).toBe(1)
  await ui.unmount()
})
