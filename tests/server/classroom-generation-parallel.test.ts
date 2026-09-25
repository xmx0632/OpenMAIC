import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  resolveModel: vi.fn(),
  isProviderKeyRequired: vi.fn(),
  generateSceneOutlinesFromRequirements: vi.fn(),
  applyOutlineFallbacks: vi.fn(),
  generateSceneContent: vi.fn(),
  generateSceneActions: vi.fn(),
  createSceneWithActions: vi.fn(),
  persistClassroom: vi.fn(),
  callLLM: vi.fn(),
}));
const PBLGenerationErrorMock = vi.hoisted(
  () =>
    class PBLGenerationError extends Error {
      readonly statusCode?: number;

      constructor(message: string, options?: { statusCode?: number }) {
        super(message);
        this.name = 'PBLGenerationError';
        this.statusCode = options?.statusCode;
      }
    },
);

vi.mock('@/lib/server/resolve-model', () => ({
  resolveModel: mocks.resolveModel,
}));

vi.mock('@/lib/ai/providers', async (importOriginal) => ({
  // The module graph now reaches the settings store (stage store -> settings),
  // whose init reads PROVIDERS - keep the real exports and stub only the probe.
  ...(await importOriginal<typeof import('@/lib/ai/providers')>()),
  isProviderKeyRequired: mocks.isProviderKeyRequired,
}));

vi.mock('@/lib/ai/llm', () => ({
  callLLM: mocks.callLLM,
}));

vi.mock('@openmaic/generation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@openmaic/generation')>()),
  generateSceneOutlinesFromRequirements: mocks.generateSceneOutlinesFromRequirements,
  applyOutlineFallbacks: mocks.applyOutlineFallbacks,
  generateSceneContent: mocks.generateSceneContent,
  generateSceneActions: mocks.generateSceneActions,
  PBLGenerationError: PBLGenerationErrorMock,
}));

vi.mock('@/lib/server/scene-generation', () => ({
  createSceneWithActions: mocks.createSceneWithActions,
}));

vi.mock('@/lib/server/classroom-storage', () => ({
  persistClassroom: mocks.persistClassroom,
}));

vi.mock('@/lib/logger', () => ({
  createLogger: () => ({
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  }),
}));

const slideContent = {
  elements: [],
  remark: 'ok',
};

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

type TestOutline = {
  id: string;
  type: string;
  title: string;
  description: string;
  keyPoints: string[];
  order: number;
  pblConfig?: {
    projectTopic: string;
    projectDescription: string;
    targetSkills: string[];
  };
};

function makeOutlines(count: number): TestOutline[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `outline-${i + 1}`,
    type: 'slide',
    title: `Scene ${i + 1}`,
    description: `Scene ${i + 1} description`,
    keyPoints: [`Point ${i + 1}`],
    order: i,
  }));
}

/** Track how many generateSceneContent calls are in flight at once. */
function trackInFlight() {
  const state = { inFlight: 0, maxInFlight: 0, completionOrder: [] as string[] };
  return {
    state,
    wrap(fn: (outline: { id: string }) => Promise<typeof slideContent | null>) {
      return async (outline: { id: string }) => {
        state.inFlight += 1;
        state.maxInFlight = Math.max(state.maxInFlight, state.inFlight);
        try {
          return await fn(outline);
        } finally {
          state.inFlight -= 1;
          state.completionOrder.push(outline.id);
        }
      };
    },
  };
}

async function generateWithProgress() {
  const progress: Array<{ message: string }> = [];
  const { generateClassroom } = await import('@/lib/server/classroom-generation');
  const result = await generateClassroom(
    { requirement: 'Teach parallel generation' },
    {
      baseUrl: 'http://localhost',
      onProgress: (event) => {
        progress.push({ message: event.message });
      },
    },
  );
  return { result, progress };
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) {
    mock.mockReset();
  }
  mocks.resolveModel.mockResolvedValue({
    model: { id: 'language-model' },
    modelInfo: {},
    modelString: 'test:model',
    providerId: 'test',
    apiKey: '',
  });
  mocks.isProviderKeyRequired.mockReturnValue(false);
  mocks.callLLM.mockResolvedValue({ text: 'ok' });
  mocks.applyOutlineFallbacks.mockImplementation((value) => value);
  mocks.generateSceneActions.mockResolvedValue([]);
  mocks.createSceneWithActions.mockImplementation((sceneOutline, content, actions, api) => {
    const sceneResult = api.scene.create({
      type: sceneOutline.type,
      title: sceneOutline.title,
      order: sceneOutline.order,
      content: {
        type: 'slide',
        canvas: {
          id: `slide-${sceneOutline.order}`,
          viewportSize: 1000,
          viewportRatio: 0.5625,
          elements: content.elements,
        },
      },
      actions,
    });
    return sceneResult.success ? (sceneResult.data ?? null) : null;
  });
  mocks.persistClassroom.mockImplementation(async ({ id, scenes }) => ({
    id,
    url: `http://localhost/classroom/${id}`,
    scenesCount: scenes.length,
    createdAt: '2026-09-26T00:00:00.000Z',
  }));
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('classroom scene generation with server-side bounded concurrency', () => {
  it('caps in-flight scene content calls at PARALLEL_SCENE_CONCURRENCY', async () => {
    vi.stubEnv('PARALLEL_SCENE_CONCURRENCY', '2');
    const outlines = makeOutlines(6);
    mocks.generateSceneOutlinesFromRequirements.mockResolvedValue({
      success: true,
      data: { languageDirective: 'Use English.', outlines },
    });
    const tracker = trackInFlight();
    mocks.generateSceneContent.mockImplementation(
      tracker.wrap(async () => {
        await delay(25);
        return slideContent;
      }),
    );

    const { result } = await generateWithProgress();

    expect(result.scenesCount).toBe(6);
    expect(tracker.state.maxInFlight).toBe(2);
  });

  it('persists scenes in outline order even when later scenes finish first', async () => {
    vi.stubEnv('PARALLEL_SCENE_CONCURRENCY', '3');
    const outlines = makeOutlines(5);
    mocks.generateSceneOutlinesFromRequirements.mockResolvedValue({
      success: true,
      data: { languageDirective: 'Use English.', outlines },
    });
    const tracker = trackInFlight();
    mocks.generateSceneContent.mockImplementation(
      tracker.wrap(async (outline) => {
        // Earlier outlines are slower, so completion order is reversed.
        const index = Number(outline.id.split('-')[1]) - 1;
        await delay(40 - index * 8);
        return slideContent;
      }),
    );

    const { result } = await generateWithProgress();

    // Concurrency actually happened: at least one later scene finished first.
    expect(tracker.state.completionOrder).not.toEqual(outlines.map((o) => o.id));
    // But insertion/persist order is still the outline order.
    expect(result.scenes.map((scene) => scene.title)).toEqual(outlines.map((o) => o.title));
    expect(mocks.createSceneWithActions.mock.calls.map(([o]) => o.title)).toEqual(
      outlines.map((o) => o.title),
    );
  });

  it('stays strictly serial when PARALLEL_SCENE_CONCURRENCY is unset', async () => {
    const outlines = makeOutlines(4);
    mocks.generateSceneOutlinesFromRequirements.mockResolvedValue({
      success: true,
      data: { languageDirective: 'Use English.', outlines },
    });
    const tracker = trackInFlight();
    mocks.generateSceneContent.mockImplementation(
      tracker.wrap(async () => {
        await delay(10);
        return slideContent;
      }),
    );

    const { result } = await generateWithProgress();

    expect(result.scenesCount).toBe(4);
    expect(tracker.state.maxInFlight).toBe(1);
    expect(tracker.state.completionOrder).toEqual(outlines.map((o) => o.id));
  });

  it('rejects with the fatal scene error and keeps earlier scenes, in order', async () => {
    vi.stubEnv('PARALLEL_SCENE_CONCURRENCY', '2');
    const outlines = makeOutlines(5);
    mocks.generateSceneOutlinesFromRequirements.mockResolvedValue({
      success: true,
      data: { languageDirective: 'Use English.', outlines },
    });
    const unauthorized = Object.assign(new Error('Unauthorized'), { statusCode: 401 });
    mocks.generateSceneContent.mockImplementation(async (outline) => {
      if (outline.id === 'outline-3') throw unauthorized;
      await delay(5);
      return slideContent;
    });

    await expect(generateWithProgress()).rejects.toBe(unauthorized);

    // Scenes before the failed outline were still created, in outline order;
    // the failed scene and everything after it never reached the store.
    expect(mocks.createSceneWithActions.mock.calls.map(([o]) => o.title)).toEqual([
      'Scene 1',
      'Scene 2',
    ]);
  });

  it('skips a PBL-failing scene under concurrency and completes the rest in order', async () => {
    vi.stubEnv('PARALLEL_SCENE_CONCURRENCY', '3');
    const outlines = makeOutlines(4);
    outlines[1] = {
      ...outlines[1],
      type: 'pbl',
      pblConfig: {
        projectTopic: 'Parallel',
        projectDescription: 'Practice concurrent generation',
        targetSkills: ['Concurrency'],
      },
    };
    mocks.generateSceneOutlinesFromRequirements.mockResolvedValue({
      success: true,
      data: { languageDirective: 'Use English.', outlines },
    });
    mocks.generateSceneContent.mockImplementation(async (outline) => {
      if (outline.type === 'pbl') throw new PBLGenerationErrorMock('both planners failed');
      await delay(5);
      return slideContent;
    });

    const { result } = await generateWithProgress();

    expect(result.scenesCount).toBe(3);
    expect(result.scenes.map((scene) => scene.title)).toEqual(['Scene 1', 'Scene 3', 'Scene 4']);
  });
});
