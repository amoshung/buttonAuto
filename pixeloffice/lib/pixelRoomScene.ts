import type { InboxItem, TeamCriterion, TeamEvent, TeamRoom } from './types';
import {
  findNavPath,
  getPetNativePosture,
  peekRoomPhysics,
  stepRoomPhysics,
  type HumanPosturePhase,
  type PetBehaviorState,
  type QuadrupedGaitState,
  type RoomPhysicsIntent,
} from './roomPhysics';
import {
  computeSolarLight,
  drawAccurateClock,
  drawCoffeeSteam,
  drawDynamicHearthFire,
  drawRichWindowAndSunShafts,
  drawSeasonalIndoorCushions,
  pickWindowVista,
  type SolarLighting,
} from './roomAtmosphere';

export const LW = 240;
export const LH = 135;
export const PIXEL_SCALE = 4; // 960x540 高精細 16:9 等距像素網格（1 邏輯格 = 4x4 精細像素點）

// 45° 等距無前牆古民家工作室（Isometric Cutaway）空間錨點（經 240x135 網格與椅背遮罩精準對位）
export const SPOT: Record<string, { x: number; y: number; pose: string }> = {
  implSeat: { x: 98.5, y: 82.5, pose: 'sit' },
  revSeat: { x: 124, y: 69.5, pose: 'sit' },
  implStand: { x: 101, y: 86, pose: 'hand' },
  revStand: { x: 126, y: 74, pose: 'hand' },
  board: { x: 145, y: 68, pose: 'write' },
  boardSide: { x: 152, y: 73, pose: 'stand' },
  implSide: { x: 106, y: 86, pose: 'stand' },
  revSide: { x: 132, y: 74, pose: 'stand' },
  bookshelf: { x: 74, y: 78, pose: 'stand' },
  center: { x: 112, y: 88, pose: 'stand' },
  test: { x: 84, y: 92, pose: 'stand' },
  sofaA: { x: 123, y: 85, pose: 'sofa' },
  sofaB: { x: 123, y: 100, pose: 'sofa' },
  coffeeA: { x: 152, y: 85, pose: 'mug' },
  coffeeB: { x: 164, y: 82, pose: 'mug' },
  cooler: { x: 152, y: 100, pose: 'stand' },
  door: { x: 174, y: 90, pose: 'walk' },
};

/**
 * 依季節切換座墊與空間錨點：
 * 冬天格紋暖桌（被爐）面積較大，四張紫色座墊向外擴張；夏天和室木茶几略偏前；春秋沿用圍爐裏座標。
 */
export function getSeasonalSpots(season: 'spring' | 'summer' | 'autumn' | 'winter'): Record<string, { x: number; y: number; pose: string }> {
  if (season === 'winter') {
    return {
      ...SPOT,
      sofaA: { x: 114, y: 81.5, pose: 'sofa' },
      sofaB: { x: 115, y: 99.5, pose: 'sofa' },
      coffeeA: { x: 156, y: 80.5, pose: 'mug' },
      cooler: { x: 154, y: 99.5, pose: 'stand' },
    };
  }
  if (season === 'summer') {
    return {
      ...SPOT,
      sofaA: { x: 122, y: 82.0, pose: 'sofa' },
      sofaB: { x: 123, y: 98.0, pose: 'sofa' },
      coffeeA: { x: 152, y: 82.0, pose: 'mug' },
      cooler: { x: 152, y: 98.0, pose: 'stand' },
    };
  }
  return SPOT;
}

export const SEAT_META: Record<string, { c: string; d: string; g: string; name: string }> = {
  kiro: { c: '#a77bf3', d: '#7652c4', g: 'K', name: 'Kiro' },
  agy: { c: '#3fc8e0', d: '#228ea3', g: 'A', name: 'Antigravity' },
  claude: { c: '#e2814f', d: '#a95a33', g: 'C', name: 'Claude' },
  glm: { c: '#62d07f', d: '#3a9a56', g: 'G', name: 'GLM-5.3' },
  glm_high: { c: '#2f8f52', d: '#1d6337', g: 'G+', name: 'GLM 高思考' },
  litellm: { c: '#f59e0b', d: '#b45309', g: 'L', name: 'LiteLLM' },
  auto: { c: '#64748b', d: '#475569', g: 'D1', name: '自動派工' },
};

export type PersonaId = 'po' | 'tech_lead' | 'scrum' | 'qa' | 'implementer' | 'fixer' | 'reviewer';
export type PersonaTier = 'supervisor' | 'developer';

export interface PersonaSpec {
  id: PersonaId;
  title: string;
  tier: PersonaTier;
  shirt: string;
  defaultEngine: string;
  gender: 'M' | 'F';
  style: string;
  defaultSay: string;
}

export const PERSONA_CATALOG: Record<PersonaId, PersonaSpec> = {
  po: {
    id: 'po',
    title: '高層主管',
    tier: 'supervisor',
    shirt: '#7aa7d9',
    defaultEngine: 'Decision-1 ＋ GLM-5.3',
    gender: 'F',
    style: 'bob',
    defaultSay: '雙模型協作：把目標拆成可驗收條件並定錨方向',
  },
  tech_lead: {
    id: 'tech_lead',
    title: '高層主管',
    tier: 'supervisor',
    shirt: '#c9b37e',
    defaultEngine: 'Decision-1 ＋ GLM-5.3',
    gender: 'M',
    style: 'spiky',
    defaultSay: '雙模型協作：規劃實作步驟與架構方向',
  },
  scrum: {
    id: 'scrum',
    title: '高層主管',
    tier: 'supervisor',
    shirt: '#b98ab0',
    defaultEngine: 'Decision-1 ＋ GLM-5.3',
    gender: 'F',
    style: 'bun',
    defaultSay: '雙模型協作：盯進度、拍板方向與權限把關',
  },
  qa: {
    id: 'qa',
    title: '高層主管',
    tier: 'supervisor',
    shirt: '#8fc7b8',
    defaultEngine: 'Decision-1 ＋ GLM-5.3',
    gender: 'F',
    style: 'bob',
    defaultSay: '雙模型協作：確認測試關卡與品質驗收',
  },
  implementer: {
    id: 'implementer',
    title: '研發工程師',
    tier: 'developer',
    shirt: '#e06d3b',
    defaultEngine: '研發代理',
    gender: 'M',
    style: 'spiky',
    defaultSay: '正在執行 TDD 測試、撰寫與精簡程式碼',
  },
  fixer: {
    id: 'fixer',
    title: '研發工程師',
    tier: 'developer',
    shirt: '#f59e0b',
    defaultEngine: '研發代理',
    gender: 'M',
    style: 'cap',
    defaultSay: '依據審查定位與主管方向決定最佳修正改法',
  },
  reviewer: {
    id: 'reviewer',
    title: '審查工程師',
    tier: 'developer',
    shirt: '#62d07f',
    defaultEngine: '審查代理',
    gender: 'F',
    style: 'bun',
    defaultSay: '盤中增量防偏離檢查（Decision-1）與唯讀獨立審查',
  },
};

export type PetBreedTrait =
  | 'corgi_sploot'
  | 'herding_dash'
  | 'scent_fetch'
  | 'husky_howl'
  | 'poodle_prance'
  | 'shiba_drill'
  | 'cat_loaf'
  | 'cat_pounce'
  | 'cat_groom'
  | 'munchkin_stand';

export interface CharacterProfile {
  id: string;
  name: string;
  bio: string;
  species: 'human' | 'animal';
  petSubspecies?: 'dog' | 'cat';
  breedTrait?: PetBreedTrait;
  sheet: 'humans' | 'humansTw' | 'dogs' | 'cats';
  cellIndex: number; // 0..11 in 4x3 sprite sheet
  sizeScale: number; // 人類為 0.94~1.02，天然犬貓為 0.42~0.56（四足犬貓真實體型比例）
  shirt: string;
  hair: string;
  skin: string;
  style: string;
  accessory: string;
}

/**
 * 48 位多樣化角色與品種名冊：
 * - 24 位人類工作角色（12 位台灣在地特色角色 ＋ 12 位日系職人角色，100% 擔任實際開發與管理職務）
 * - 24 種純天然四足犬貓吉祥物（12 種狗狗品種 ＋ 12 種貓咪品種，嚴禁任何動物擬人化／穿衣／雙足站立，具備品種專屬行為與物理互動！）
 */
export const CHARACTER_ROSTER: CharacterProfile[] = [
  // === 12 位台灣在地特色人類角色（sheet: 'humansTw'，優先登場） ===
  {
    id: 'tw01_hsinchu',
    name: '阿哲',
    bio: '竹科手搖飲資深工程師',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 0,
    sizeScale: 1.0,
    shirt: '#243652',
    hair: '#22242a',
    skin: '#f2c8a2',
    style: 'short',
    accessory: 'boba_lanyard',
  },
  {
    id: 'tw02_electrician',
    name: '阿欽伯',
    bio: '台灣水電工地主任架構師',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 1,
    sizeScale: 1.02,
    shirt: '#325d8c',
    hair: '#3a3c42',
    skin: '#dfaf87',
    style: 'short',
    accessory: 'neck_towel',
  },
  {
    id: 'tw03_nightmarket',
    name: '雅婷',
    bio: '夜市美食達人前端工程師',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 2,
    sizeScale: 0.96,
    shirt: '#b8332a',
    hair: '#4d3324',
    skin: '#f5cfa8',
    style: 'bun',
    accessory: 'apron_skewer',
  },
  {
    id: 'tw04_temple',
    name: '家豪',
    bio: '宮廟文創潮牌全端設計師',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 3,
    sizeScale: 1.0,
    shirt: '#c92a2a',
    hair: '#33221a',
    skin: '#eec098',
    style: 'cap',
    accessory: 'temple_cap',
  },
  {
    id: 'tw05_gazi',
    name: '詩涵',
    bio: '大稻埕茄芷袋文青產品經理',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 4,
    sizeScale: 0.96,
    shirt: '#9c7e54',
    hair: '#543824',
    skin: '#f6d2ad',
    style: 'bob',
    accessory: 'gazi_bag',
  },
  {
    id: 'tw06_railway',
    name: '鐵雄',
    bio: '台鐵鐵道迷高併發調度官',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 5,
    sizeScale: 1.0,
    shirt: '#23314d',
    hair: '#2b2019',
    skin: '#f0c49d',
    style: 'cap',
    accessory: 'railway_cap',
  },
  {
    id: 'tw07_yushan',
    name: '山羌哥',
    bio: '台灣百岳登山 SRE 維運長',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 6,
    sizeScale: 1.01,
    shirt: '#1b7a78',
    hair: '#38291e',
    skin: '#e6b891',
    style: 'cap',
    accessory: 'hiking_hat',
  },
  {
    id: 'tw08_scooter',
    name: '冠宇',
    bio: '西瓜皮安全帽機車通勤駭客',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 7,
    sizeScale: 1.0,
    shirt: '#b03028',
    hair: '#3a271c',
    skin: '#edb991',
    style: 'cap',
    accessory: 'scooter_helmet',
  },
  {
    id: 'tw09_hakka',
    name: '曉彤',
    bio: '客家花布美學 UI/UX 總監',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 8,
    sizeScale: 0.95,
    shirt: '#32689a',
    hair: '#422c1f',
    skin: '#f5cfa8',
    style: 'bun',
    accessory: 'hakka_floral',
  },
  {
    id: 'tw10_indigenous',
    name: '巴奈',
    bio: '原住民圖騰編織資安守門員',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 9,
    sizeScale: 1.0,
    shirt: '#a82c24',
    hair: '#241a14',
    skin: '#d1986e',
    style: 'short',
    accessory: 'totem_headband',
  },
  {
    id: 'tw11_tainan',
    name: '阿甘叔',
    bio: '台南老宅藍白拖功夫茶 QA',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 10,
    sizeScale: 0.98,
    shirt: '#cbb89d',
    hair: '#a6a8b0',
    skin: '#e8bc96',
    style: 'short',
    accessory: 'teapot_slippers',
  },
  {
    id: 'tw12_mart',
    name: '小綠',
    bio: '台灣超商萬能店長 Scrum 教練',
    species: 'human',
    sheet: 'humansTw',
    cellIndex: 11,
    sizeScale: 0.98,
    shirt: '#2b9c46',
    hair: '#29221e',
    skin: '#f2c8a2',
    style: 'short',
    accessory: 'mart_vest',
  },

  // === 12 位日系古民家職人角色（sheet: 'humans'） ===
  {
    id: 'h01_backpacker',
    name: '阿嵐',
    bio: '登山背包客工程師',
    species: 'human',
    sheet: 'humans',
    cellIndex: 0,
    sizeScale: 1.0,
    shirt: '#e86a2c',
    hair: '#4a3222',
    skin: '#f4cca4',
    style: 'short',
    accessory: 'backpack',
  },
  {
    id: 'h02_sensei',
    name: '健太先生',
    bio: '圓框眼鏡技術導師',
    species: 'human',
    sheet: 'humans',
    cellIndex: 1,
    sizeScale: 1.0,
    shirt: '#c7a477',
    hair: '#3b281e',
    skin: '#f1c7a5',
    style: 'short',
    accessory: 'glasses',
  },
  {
    id: 'h03_craftsman',
    name: '蒼真',
    bio: '藍染羽織系統職人',
    species: 'human',
    sheet: 'humans',
    cellIndex: 2,
    sizeScale: 1.0,
    shirt: '#2c5270',
    hair: '#1f242d',
    skin: '#e8b993',
    style: 'short',
    accessory: 'haori',
  },
  {
    id: 'h04_miko',
    name: '小葵',
    bio: '神社巫女演算法師',
    species: 'human',
    sheet: 'humans',
    cellIndex: 3,
    sizeScale: 0.96,
    shirt: '#d63e34',
    hair: '#1c1c24',
    skin: '#f7d5b4',
    style: 'bob',
    accessory: 'ribbon',
  },
  {
    id: 'h05_barista',
    name: '悠斗',
    bio: '手沖咖啡全端職人',
    species: 'human',
    sheet: 'humans',
    cellIndex: 4,
    sizeScale: 0.98,
    shirt: '#b89f7d',
    hair: '#5c3c24',
    skin: '#f1c7a5',
    style: 'cap',
    accessory: 'beret',
  },
  {
    id: 'h06_farmer',
    name: '源叔',
    bio: '田舍斗笠資深維運',
    species: 'human',
    sheet: 'humans',
    cellIndex: 5,
    sizeScale: 1.02,
    shirt: '#456b8c',
    hair: '#33241b',
    skin: '#deb087',
    style: 'cap',
    accessory: 'straw_hat',
  },
  {
    id: 'h07_student',
    name: '凜',
    bio: '雙馬尾天才實習生',
    species: 'human',
    sheet: 'humans',
    cellIndex: 6,
    sizeScale: 0.94,
    shirt: '#2c3a57',
    hair: '#573a28',
    skin: '#f6d2b0',
    style: 'bob',
    accessory: 'sailor',
  },
  {
    id: 'h08_ramen',
    name: '大將',
    bio: '熱血鉢卷重構達人',
    species: 'human',
    sheet: 'humans',
    cellIndex: 7,
    sizeScale: 1.02,
    shirt: '#283347',
    hair: '#241c18',
    skin: '#e5b58d',
    style: 'spiky',
    accessory: 'headband',
  },
  {
    id: 'h09_artist',
    name: '美咲',
    bio: '京都貝雷帽設計師',
    species: 'human',
    sheet: 'humans',
    cellIndex: 8,
    sizeScale: 0.96,
    shirt: '#d99b38',
    hair: '#63412c',
    skin: '#f4cca4',
    style: 'bob',
    accessory: 'glasses',
  },
  {
    id: 'h10_innkeeper',
    name: '哲也',
    bio: '溫泉旅館架構掌櫃',
    species: 'human',
    sheet: 'humans',
    cellIndex: 9,
    sizeScale: 1.0,
    shirt: '#6ba8a9',
    hair: '#36261d',
    skin: '#f1c7a5',
    style: 'short',
    accessory: 'yukata',
  },
  {
    id: 'h11_postman',
    name: '阿誠',
    bio: '復古郵差 API 特派員',
    species: 'human',
    sheet: 'humans',
    cellIndex: 10,
    sizeScale: 1.0,
    shirt: '#2e6f40',
    hair: '#3a271c',
    skin: '#f0c299',
    style: 'cap',
    accessory: 'mailbag',
  },
  {
    id: 'h12_grandma',
    name: '梅婆',
    bio: '茶屋資深品質長老',
    species: 'human',
    sheet: 'humans',
    cellIndex: 11,
    sizeScale: 0.92,
    shirt: '#769958',
    hair: '#c4c4cc',
    skin: '#eeddd0',
    style: 'bun',
    accessory: 'glasses',
  },

  // === 12 種純天然狗狗品種（sheet: 'dogs'，四足自然犬隻，零擬人化，顯眼清晰比例 0.73~0.85） ===
  {
    id: 'd01_formosan',
    name: '小黑',
    bio: '台灣犬（土狗）機警護衛',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'herding_dash',
    sheet: 'dogs',
    cellIndex: 0,
    sizeScale: 0.82,
    shirt: '#222428',
    hair: '#1a1c20',
    skin: '#2f3238',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd02_shiba_red',
    name: '豆助',
    bio: '赤柴犬（飛機耳搖尾）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'shiba_drill',
    sheet: 'dogs',
    cellIndex: 1,
    sizeScale: 0.78,
    shirt: '#e0822b',
    hair: '#d9721e',
    skin: '#faedcd',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd03_shiba_black',
    name: '黑豆',
    bio: '黑柴犬（白眉忠犬）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'shiba_drill',
    sheet: 'dogs',
    cellIndex: 2,
    sizeScale: 0.78,
    shirt: '#2b2623',
    hair: '#1f1b18',
    skin: '#ebd5b3',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd04_corgi',
    name: '屁屁',
    bio: '威爾斯柯基（短腿吐司臀）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'corgi_sploot',
    sheet: 'dogs',
    cellIndex: 3,
    sizeScale: 0.76,
    shirt: '#ea8a2f',
    hair: '#d8761c',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd05_golden',
    name: '金金',
    bio: '黃金獵犬（溫馴巡迴咬球）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'scent_fetch',
    sheet: 'dogs',
    cellIndex: 4,
    sizeScale: 0.85,
    shirt: '#ebbc6c',
    hair: '#d49e47',
    skin: '#f7ddb0',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd06_collie',
    name: '閃電',
    bio: '邊境牧羊犬（高智商折返跑）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'herding_dash',
    sheet: 'dogs',
    cellIndex: 5,
    sizeScale: 0.82,
    shirt: '#23272e',
    hair: '#16191f',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd07_husky',
    name: '二哈',
    bio: '西伯利亞哈士奇（狼嚎邀玩）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'husky_howl',
    sheet: 'dogs',
    cellIndex: 6,
    sizeScale: 0.84,
    shirt: '#8b95a1',
    hair: '#626c78',
    skin: '#f5f7fa',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd08_frenchie',
    name: '肉包',
    bio: '奶油法國鬥牛犬（蝙蝠耳趴睡）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'corgi_sploot',
    sheet: 'dogs',
    cellIndex: 7,
    sizeScale: 0.75,
    shirt: '#cfa676',
    hair: '#38302a',
    skin: '#dec099',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd09_dachshund',
    name: '熱狗',
    bio: '短毛臘腸犬（嗅聞地板巡視）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'scent_fetch',
    sheet: 'dogs',
    cellIndex: 8,
    sizeScale: 0.74,
    shirt: '#7a3e20',
    hair: '#5c2b12',
    skin: '#9e5831',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd10_poodle',
    name: '紅貴賓',
    bio: '泰迪紅貴賓（輕快小碎步）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'poodle_prance',
    sheet: 'dogs',
    cellIndex: 9,
    sizeScale: 0.74,
    shirt: '#bf5226',
    hair: '#a13e18',
    skin: '#d46538',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd11_bichon',
    name: '棉花糖',
    bio: '比熊犬（蓬鬆白雲彈跳）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'poodle_prance',
    sheet: 'dogs',
    cellIndex: 10,
    sizeScale: 0.74,
    shirt: '#f2f5f8',
    hair: '#ffffff',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'd12_beagle',
    name: '米格魯',
    bio: '三色米格魯（垂耳嗅聞獵犬）',
    species: 'animal',
    petSubspecies: 'dog',
    breedTrait: 'scent_fetch',
    sheet: 'dogs',
    cellIndex: 11,
    sizeScale: 0.77,
    shirt: '#c7722c',
    hair: '#232429',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },

  // === 12 種純天然貓咪品種（sheet: 'cats'，四足自然貓咪，零擬人化，顯眼清晰比例 0.70~0.78） ===
  {
    id: 'c01_tabby_tw',
    name: '斑斑',
    bio: '台灣虎斑米克斯（壓低搖屁股撲球）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_pounce',
    sheet: 'cats',
    cellIndex: 0,
    sizeScale: 0.75,
    shirt: '#8c6d4e',
    hair: '#382a1d',
    skin: '#b59472',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c02_orange',
    name: '橘座',
    bio: '台灣大橘貓（折手農夫揣呼嚕）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_loaf',
    sheet: 'cats',
    cellIndex: 1,
    sizeScale: 0.78,
    shirt: '#e8832c',
    hair: '#c96414',
    skin: '#fae3c6',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c03_calico',
    name: '花花',
    bio: '三花米克斯（舔爪洗臉理毛）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_groom',
    sheet: 'cats',
    cellIndex: 2,
    sizeScale: 0.74,
    shirt: '#e07a2b',
    hair: '#292522',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c04_black_cat',
    name: '歐嚕嚕',
    bio: '台灣黑貓（黃眼潛行狩獵）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_pounce',
    sheet: 'cats',
    cellIndex: 3,
    sizeScale: 0.74,
    shirt: '#1c1d22',
    hair: '#121317',
    skin: '#292b33',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c05_tuxedo',
    name: '總裁',
    bio: '黑白賓士貓（白襪端坐理毛）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_groom',
    sheet: 'cats',
    cellIndex: 4,
    sizeScale: 0.75,
    shirt: '#22242a',
    hair: '#15171c',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c06_tortie',
    name: '琥珀',
    bio: '玳瑁貓（琥珀斑紋踏踏）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_loaf',
    sheet: 'cats',
    cellIndex: 5,
    sizeScale: 0.74,
    shirt: '#4a3222',
    hair: '#211813',
    skin: '#d97a2b',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c07_siamese',
    name: '焦糖',
    bio: '暹羅貓（藍眼重點色優雅散步）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_groom',
    sheet: 'cats',
    cellIndex: 6,
    sizeScale: 0.74,
    shirt: '#e6d5b8',
    hair: '#422c20',
    skin: '#f5ead6',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c08_british_blue',
    name: '藍胖',
    bio: '英國短毛藍貓（圓臉揣手呼嚕）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_loaf',
    sheet: 'cats',
    cellIndex: 7,
    sizeScale: 0.77,
    shirt: '#6e7887',
    hair: '#555e6b',
    skin: '#8590a1',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c09_am_shorthair',
    name: '銀銀',
    bio: '美國短毛銀虎斑（敏捷追球）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_pounce',
    sheet: 'cats',
    cellIndex: 8,
    sizeScale: 0.75,
    shirt: '#9aa2ad',
    hair: '#2e333b',
    skin: '#cfd5de',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c10_persian',
    name: '雪球',
    bio: '波斯白貓（蓬鬆長毛打盹）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_loaf',
    sheet: 'cats',
    cellIndex: 9,
    sizeScale: 0.76,
    shirt: '#f5f7fa',
    hair: '#ffffff',
    skin: '#ffffff',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c11_ragdoll',
    name: '布丁',
    bio: '雙色布偶貓（溫馴翻肚撒嬌）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'cat_groom',
    sheet: 'cats',
    cellIndex: 10,
    sizeScale: 0.77,
    shirt: '#ede3d2',
    hair: '#695649',
    skin: '#faf6f0',
    style: 'ears',
    accessory: 'none',
  },
  {
    id: 'c12_munchkin',
    name: '短腿喵',
    bio: '曼赤肯短腿貓（好奇小碎步）',
    species: 'animal',
    petSubspecies: 'cat',
    breedTrait: 'munchkin_stand',
    sheet: 'cats',
    cellIndex: 11,
    sizeScale: 0.72,
    shirt: '#f09c54',
    hair: '#d47c32',
    skin: '#fff5e6',
    style: 'ears',
    accessory: 'none',
  },
];

export interface CompanionSpec {
  id: 'shiba' | 'calico';
  name: string;
  species: 'dog' | 'cat';
  x: number;
  y: number;
  pose?: string;
  facing?: 'left' | 'right' | 'front';
  facingLeft?: boolean;
  vx?: number;
  vy?: number;
  behavior?: PetBehaviorState;
  gait?: QuadrupedGaitState;
  interactionHeart?: boolean;
  defaultAction: string;
  character?: CharacterProfile;
}

export const COMPANIONS: CompanionSpec[] = [
  {
    id: 'shiba',
    name: '台灣犬小黑',
    species: 'dog',
    x: 82,
    y: 98,
    defaultAction: '搖尾巴陪伴開發中 ♪',
    character: CHARACTER_ROSTER.find((c) => c.id === 'd01_formosan') || CHARACTER_ROSTER[24],
  },
  {
    id: 'calico',
    name: '台灣虎斑貓斑斑',
    species: 'cat',
    x: 152,
    y: 100,
    defaultAction: '在圍爐裏坐墊呼嚕熟睡 zZ',
    character: CHARACTER_ROSTER.find((c) => c.id === 'c01_tabby_tw') || CHARACTER_ROSTER[36],
  },
];

// 記錄每個房間的隨機換角次數，以及目前畫面上可見房間的順位表（確保單一房間與多房間同畫面皆不重複角色）
const roomCastSeeds = new Map<string, number>();
const visibleRoomOrder: string[] = [];

export function registerVisibleRooms(roomIds: string[]) {
  visibleRoomOrder.length = 0;
  roomIds.forEach((id) => {
    if (!visibleRoomOrder.includes(id)) visibleRoomOrder.push(id);
  });
}

export function getRoomCastSeed(roomId: string): number {
  return roomCastSeeds.get(roomId) || 0;
}

export function cycleRoomCast(roomId: string): number {
  const next = (roomCastSeeds.get(roomId) || 0) + 1;
  roomCastSeeds.set(roomId, next);
  return next;
}

/**
 * 從 48 位角色庫（24 位人類工作角色 + 24 種純天然犬貓品種）中，為指定房間選出 5 位 100% 不重複的角色：
 * - 3 位實際工作角色（supervisor, implementer, reviewer）：100% 全部使用「人類（human）」，並優先穿插台灣在地角色與日系職人！
 * - 2 位房間吉祥物夥伴（companions）：100% 全部使用「純天然犬貓（1 狗 + 1 貓）」，絕無擬人穿衣，且體型符合四足寵物比例！
 * - 單一房間與多房間同畫面（如 2x2 四格或 3x3 九格）透過無碰撞分區，確保同畫面絕不出現重複角色或重複犬貓品種！
 */
export function pickRoomCast(roomId: string, extraSeed = 0): {
  supervisor: CharacterProfile;
  implementer: CharacterProfile;
  reviewer: CharacterProfile;
  companions: [CharacterProfile, CharacterProfile];
} {
  const twHumans = CHARACTER_ROSTER.filter((c) => c.species === 'human' && c.sheet === 'humansTw'); // 12 位台灣特色人類
  const jpHumans = CHARACTER_ROSTER.filter((c) => c.species === 'human' && c.sheet === 'humans'); // 12 位日系職人人類
  // 交錯排列台灣與日系人類（24 位人類），讓每個房間都至少有 1~2 位台灣特色角色登場！
  const interleavedHumans: CharacterProfile[] = [];
  const maxH = Math.max(twHumans.length, jpHumans.length);
  for (let i = 0; i < maxH; i++) {
    if (i < twHumans.length) interleavedHumans.push(twHumans[i]);
    if (i < jpHumans.length) interleavedHumans.push(jpHumans[i]);
  }

  const dogs = CHARACTER_ROSTER.filter((c) => c.species === 'animal' && c.petSubspecies === 'dog'); // 12 種狗狗品種
  const cats = CHARACTER_ROSTER.filter((c) => c.species === 'animal' && c.petSubspecies === 'cat'); // 12 種貓咪品種

  const slotIdx = visibleRoomOrder.indexOf(roomId);
  const castRound = getRoomCastSeed(roomId) + extraSeed;

  // 每個房間分配 3 位不重複的人類擔任工作角色（gcd(5, 24) = 1，確保每次點擊換角都完全換一批人）
  const hBase =
    slotIdx >= 0
      ? (slotIdx * 3 + castRound * 5) % interleavedHumans.length
      : ((mix(hash(roomId)) % 8) * 3 + castRound * 5) % interleavedHumans.length;
  const supHuman = interleavedHumans[hBase % interleavedHumans.length];
  const implHuman = interleavedHumans[(hBase + 1) % interleavedHumans.length];
  const revHuman = interleavedHumans[(hBase + 2) % interleavedHumans.length];

  // 每個房間分配 1 隻狗狗品種 + 1 隻貓咪品種擔任吉祥物（gcd(5, 12) = 1，確保同畫面最多 12 個房間都不重複犬貓！）
  const dIdx =
    slotIdx >= 0
      ? (slotIdx + castRound * 5) % dogs.length
      : ((mix(hash(roomId)) % dogs.length) + castRound * 5) % dogs.length;
  const cIdx =
    slotIdx >= 0
      ? (slotIdx + castRound * 7) % cats.length
      : ((mix(hash(`${roomId}#cat`)) % cats.length) + castRound * 7) % cats.length;

  return {
    supervisor: supHuman,
    implementer: implHuman,
    reviewer: revHuman,
    companions: [dogs[dIdx], cats[cIdx]],
  };
}

export interface RoomThemeSpec {
  id: string;
  name: string;
  badge: string;
  season: 'spring' | 'summer' | 'autumn' | 'winter';
  weather: 'sakura' | 'rain' | 'sunny' | 'thunder' | 'seaside' | 'firefly' | 'maple' | 'harvest' | 'ginkgo' | 'mist' | 'snow' | 'blizzard' | 'onsen' | 'sunset' | 'starry' | 'bamboo';
  skyTop: string;
  skyBottom: string;
  hillFar: string;
  hillNear: string;
  foliagePrimary: string;
  foliageSecondary: string;
  shojiTint: string;
  indoorAmbient: string;
  cushionMain: string;
  cushionTrim: string;
  decorProp: 'sakura_tea' | 'rain_umbrella' | 'watermelon' | 'thunder_lantern' | 'surfboard' | 'andon_lamp' | 'sweet_potato' | 'harvest_moon' | 'vinyl_ginkgo' | 'zen_bonsai' | 'kotatsu' | 'blizzard_hearth' | 'onsen_bucket' | 'sunset_easel' | 'telescope' | 'bamboo_garden';
}

/**
 * 16 種房間風景與屋內擺設主題（涵蓋春夏秋冬、下雨、打雷閃電、吹雪、星夜、海濱、竹林等，且屋內擺設與坐墊隨主題變換！）
 */
export const ROOM_THEMES: RoomThemeSpec[] = [
  {
    id: 'spring_sakura',
    name: '春櫻漫舞',
    badge: '🌸 春櫻漫舞',
    season: 'spring',
    weather: 'sakura',
    skyTop: '#7ec8e3',
    skyBottom: '#fce4ec',
    hillFar: '#88bdbc',
    hillNear: '#689f38',
    foliagePrimary: '#f48fb1',
    foliageSecondary: '#f8bbd0',
    shojiTint: 'rgba(255, 218, 233, 0.16)',
    indoorAmbient: 'rgba(255, 192, 203, 0.05)',
    cushionMain: '#d87093',
    cushionTrim: '#fce4ec',
    decorProp: 'sakura_tea',
  },
  {
    id: 'spring_rain',
    name: '春雨綿綿',
    badge: '🌧️ 春雨綿綿',
    season: 'spring',
    weather: 'rain',
    skyTop: '#546e7a',
    skyBottom: '#90a4ae',
    hillFar: '#455a64',
    hillNear: '#387046',
    foliagePrimary: '#4caf50',
    foliageSecondary: '#81c784',
    shojiTint: 'rgba(144, 202, 249, 0.18)',
    indoorAmbient: 'rgba(100, 149, 237, 0.06)',
    cushionMain: '#3b7a57',
    cushionTrim: '#a5d6a7',
    decorProp: 'rain_umbrella',
  },
  {
    id: 'summer_verdant',
    name: '盛夏新綠',
    badge: '🍉 盛夏新綠',
    season: 'summer',
    weather: 'sunny',
    skyTop: '#29b6f6',
    skyBottom: '#b3e5fc',
    hillFar: '#43a047',
    hillNear: '#2e7d32',
    foliagePrimary: '#2e7d32',
    foliageSecondary: '#66bb6a',
    shojiTint: 'rgba(179, 229, 252, 0.14)',
    indoorAmbient: 'rgba(255, 245, 157, 0.05)',
    cushionMain: '#0288d1',
    cushionTrim: '#b3e5fc',
    decorProp: 'watermelon',
  },
  {
    id: 'summer_thunder',
    name: '夏夜雷雨',
    badge: '⛈️ 夏夜雷雨',
    season: 'summer',
    weather: 'thunder',
    skyTop: '#1a1c2c',
    skyBottom: '#3b3355',
    hillFar: '#262338',
    hillNear: '#1b2a2f',
    foliagePrimary: '#28354a',
    foliageSecondary: '#3f51b5',
    shojiTint: 'rgba(92, 107, 192, 0.24)',
    indoorAmbient: 'rgba(40, 53, 147, 0.12)',
    cushionMain: '#5c6bc0',
    cushionTrim: '#c5cae9',
    decorProp: 'thunder_lantern',
  },
  {
    id: 'summer_seaside',
    name: '湘南夏海',
    badge: '🌊 湘南夏海',
    season: 'summer',
    weather: 'seaside',
    skyTop: '#039be5',
    skyBottom: '#e1f5fe',
    hillFar: '#0277bd',
    hillNear: '#00acc1',
    foliagePrimary: '#26a69a',
    foliageSecondary: '#80cbc4',
    shojiTint: 'rgba(129, 212, 250, 0.16)',
    indoorAmbient: 'rgba(0, 188, 212, 0.05)',
    cushionMain: '#00838f',
    cushionTrim: '#e0f7fa',
    decorProp: 'surfboard',
  },
  {
    id: 'summer_firefly',
    name: '夏夜螢火',
    badge: '✨ 夏夜螢火',
    season: 'summer',
    weather: 'firefly',
    skyTop: '#0d1b2a',
    skyBottom: '#1b3a4b',
    hillFar: '#16262e',
    hillNear: '#1b4332',
    foliagePrimary: '#2d6a4f',
    foliageSecondary: '#74c69d',
    shojiTint: 'rgba(52, 211, 153, 0.15)',
    indoorAmbient: 'rgba(16, 185, 129, 0.06)',
    cushionMain: '#2d6a4f',
    cushionTrim: '#b7e4c7',
    decorProp: 'andon_lamp',
  },
  {
    id: 'autumn_maple',
    name: '深秋楓紅',
    badge: '🍁 深秋楓紅',
    season: 'autumn',
    weather: 'maple',
    skyTop: '#f57c00',
    skyBottom: '#ffe082',
    hillFar: '#8d6e63',
    hillNear: '#bf360c',
    foliagePrimary: '#d84315',
    foliageSecondary: '#ff8f00',
    shojiTint: 'rgba(255, 183, 77, 0.12)',
    indoorAmbient: 'rgba(245, 124, 0, 0.05)',
    cushionMain: '#b7410e',
    cushionTrim: '#ffe082',
    decorProp: 'sweet_potato',
  },
  {
    id: 'autumn_harvest',
    name: '秋收月見',
    badge: '🌕 秋收月見',
    season: 'autumn',
    weather: 'harvest',
    skyTop: '#1e293b',
    skyBottom: '#475569',
    hillFar: '#334155',
    hillNear: '#92400e',
    foliagePrimary: '#d97706',
    foliageSecondary: '#fcd34d',
    shojiTint: 'rgba(253, 224, 71, 0.16)',
    indoorAmbient: 'rgba(251, 191, 36, 0.06)',
    cushionMain: '#854d0e',
    cushionTrim: '#fef08a',
    decorProp: 'harvest_moon',
  },
  {
    id: 'autumn_ginkgo',
    name: '銀杏金風',
    badge: '🍂 銀杏金風',
    season: 'autumn',
    weather: 'ginkgo',
    skyTop: '#64b5f6',
    skyBottom: '#fff9c4',
    hillFar: '#90a4ae',
    hillNear: '#f57f17',
    foliagePrimary: '#fbc02d',
    foliageSecondary: '#fff176',
    shojiTint: 'rgba(255, 241, 118, 0.16)',
    indoorAmbient: 'rgba(251, 192, 45, 0.06)',
    cushionMain: '#d97706',
    cushionTrim: '#fef9c3',
    decorProp: 'vinyl_ginkgo',
  },
  {
    id: 'autumn_mist',
    name: '山霧朝露',
    badge: '🌫️ 山霧朝露',
    season: 'autumn',
    weather: 'mist',
    skyTop: '#78909c',
    skyBottom: '#cfd8dc',
    hillFar: '#607d8b',
    hillNear: '#455a64',
    foliagePrimary: '#546e7a',
    foliageSecondary: '#90a4ae',
    shojiTint: 'rgba(207, 216, 220, 0.20)',
    indoorAmbient: 'rgba(144, 164, 174, 0.06)',
    cushionMain: '#475569',
    cushionTrim: '#e2e8f0',
    decorProp: 'zen_bonsai',
  },
  {
    id: 'winter_snow',
    name: '寒冬瑞雪',
    badge: '❄️ 寒冬瑞雪',
    season: 'winter',
    weather: 'snow',
    skyTop: '#455a64',
    skyBottom: '#cfd8dc',
    hillFar: '#90a4ae',
    hillNear: '#eceff1',
    foliagePrimary: '#b0bec5',
    foliageSecondary: '#ffffff',
    shojiTint: 'rgba(224, 242, 254, 0.24)',
    indoorAmbient: 'rgba(186, 230, 253, 0.07)',
    cushionMain: '#b91c1c',
    cushionTrim: '#fecaca',
    decorProp: 'kotatsu',
  },
  {
    id: 'winter_blizzard',
    name: '吹雪極夜',
    badge: '🌨️ 吹雪極夜',
    season: 'winter',
    weather: 'blizzard',
    skyTop: '#0f172a',
    skyBottom: '#334155',
    hillFar: '#1e293b',
    hillNear: '#64748b',
    foliagePrimary: '#94a3b8',
    foliageSecondary: '#f8fafc',
    shojiTint: 'rgba(148, 163, 184, 0.25)',
    indoorAmbient: 'rgba(56, 189, 248, 0.08)',
    cushionMain: '#991b1b',
    cushionTrim: '#fde047',
    decorProp: 'blizzard_hearth',
  },
  {
    id: 'winter_onsen',
    name: '雪見溫泉',
    badge: '♨️ 雪見溫泉',
    season: 'winter',
    weather: 'onsen',
    skyTop: '#37474f',
    skyBottom: '#b0bec5',
    hillFar: '#78909c',
    hillNear: '#e0f2f1',
    foliagePrimary: '#c62828',
    foliageSecondary: '#ffffff',
    shojiTint: 'rgba(207, 216, 220, 0.20)',
    indoorAmbient: 'rgba(254, 215, 170, 0.08)',
    cushionMain: '#7c2d12',
    cushionTrim: '#fed7aa',
    decorProp: 'onsen_bucket',
  },
  {
    id: 'twilight_sunset',
    name: '茜色夕照',
    badge: '🌇 茜色夕照',
    season: 'autumn',
    weather: 'sunset',
    skyTop: '#4a148c',
    skyBottom: '#ff7043',
    hillFar: '#311b92',
    hillNear: '#bf360c',
    foliagePrimary: '#e64a19',
    foliageSecondary: '#ffab91',
    shojiTint: 'rgba(255, 112, 67, 0.18)',
    indoorAmbient: 'rgba(251, 146, 60, 0.08)',
    cushionMain: '#9a3412',
    cushionTrim: '#fdba74',
    decorProp: 'sunset_easel',
  },
  {
    id: 'starry_aurora',
    name: '星夜極光',
    badge: '🌌 星夜極光',
    season: 'winter',
    weather: 'starry',
    skyTop: '#090d16',
    skyBottom: '#1e1b4b',
    hillFar: '#111827',
    hillNear: '#1f2937',
    foliagePrimary: '#312e81',
    foliageSecondary: '#34d399',
    shojiTint: 'rgba(99, 102, 241, 0.22)',
    indoorAmbient: 'rgba(79, 70, 229, 0.09)',
    cushionMain: '#3730a3',
    cushionTrim: '#a5b4fc',
    decorProp: 'telescope',
  },
  {
    id: 'bamboo_zen',
    name: '嵐山竹林',
    badge: '🎋 嵐山竹林',
    season: 'spring',
    weather: 'bamboo',
    skyTop: '#a7f3d0',
    skyBottom: '#ecfdf5',
    hillFar: '#047857',
    hillNear: '#065f46',
    foliagePrimary: '#10b981',
    foliageSecondary: '#6ee7b7',
    shojiTint: 'rgba(110, 231, 183, 0.16)',
    indoorAmbient: 'rgba(16, 185, 129, 0.05)',
    cushionMain: '#15803d',
    cushionTrim: '#bbf7d0',
    decorProp: 'bamboo_garden',
  },
];

const roomThemeOverrides = new Map<string, string>();

export function setRoomThemeOverride(roomId: string, themeId: string | 'auto') {
  if (themeId === 'auto') roomThemeOverrides.delete(roomId);
  else roomThemeOverrides.set(roomId, themeId);
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(`pixeloffice:theme:${roomId}`, themeId);
    } catch {
      // ignore storage errors
    }
  }
}

export function getRoomThemeOverride(roomId: string): string {
  const cached = roomThemeOverrides.get(roomId);
  if (cached) return cached;
  if (typeof window !== 'undefined') {
    try {
      const saved = window.localStorage.getItem(`pixeloffice:theme:${roomId}`);
      if (saved && saved !== 'auto' && ROOM_THEMES.some((t) => t.id === saved)) {
        roomThemeOverrides.set(roomId, saved);
        return saved;
      }
    } catch {
      // ignore
    }
  }
  return 'auto';
}

let autoSeasonHemisphere: 'north' | 'south' = 'north';

export function setAutoSeasonHemisphere(h: 'north' | 'south') {
  autoSeasonHemisphere = h;
}

export function pickRoomTheme(roomId: string, extraSeed = 0): RoomThemeSpec {
  const overrideId = getRoomThemeOverride(roomId);
  if (overrideId !== 'auto') {
    const found = ROOM_THEMES.find((t) => t.id === overrideId);
    if (found) return found;
  }
  const realSeason = resolveSeasonForDate(new Date(), autoSeasonHemisphere);
  const seasonStartIdx = Math.max(
    0,
    ROOM_THEMES.findIndex((t) => t.season === realSeason)
  );
  const slotIdx = visibleRoomOrder.indexOf(roomId);
  const castRound = getRoomCastSeed(roomId) + extraSeed;
  const idx =
    slotIdx >= 0
      ? (seasonStartIdx + slotIdx + castRound * 3) % ROOM_THEMES.length
      : (seasonStartIdx + (castRound === 0 ? 0 : mix(hash(`${roomId}#theme`)) + castRound)) % ROOM_THEMES.length;
  return ROOM_THEMES[idx];
}

const C = {
  skin: ['#f1c7a5', '#e0ac85', '#c98f68', '#a8724f'],
  eye: '#241710',
  dark: '#241710',
  pants: '#2b3340',
  shoe: '#1c130e',
  white: '#f4f7fa',
  screenOff: '#171c24',
  bezel: '#241a14',
  heat: '#f08a24',
  red: '#e5534b',
  ok: '#57c27a',
  warn: '#e3b341',
};

const HAIR = ['#2b2118', '#4a3424', '#1c1c22', '#6b4a2e', '#8a5a2b', '#3a2a20', '#b8b0a2'];
const STYLES = ['short', 'spiky', 'bob', 'bun', 'cap', 'short'];

export function hash(s: string): number {
  let v = 7;
  for (const ch of String(s)) v = (v * 31 + ch.charCodeAt(0)) >>> 0;
  return v;
}

function mix(v: number): number {
  v ^= v >>> 16;
  v = Math.imul(v, 0x45d9f3b) >>> 0;
  v ^= v >>> 16;
  v = Math.imul(v, 0x45d9f3b) >>> 0;
  return (v ^ (v >>> 16)) >>> 0;
}

function px(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, col: string) {
  ctx.fillStyle = col;
  ctx.fillRect(Math.round(x * 2) / 2, Math.round(y * 2) / 2, Math.round(w * 2) / 2, Math.round(h * 2) / 2);
}

function fpx(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, col: string) {
  ctx.fillStyle = col;
  ctx.fillRect(
    Math.round(x * 4) / 4,
    Math.round(y * 4) / 4,
    Math.max(0.25, Math.round(w * 4) / 4),
    Math.max(0.25, Math.round(h * 4) / 4)
  );
}

export function lookOf(key: string, forceStyle?: string) {
  const v = mix(hash(key));
  return {
    hair: HAIR[v % HAIR.length],
    style: forceStyle || STYLES[(v >>> 4) % STYLES.length],
    skin: C.skin[(v >>> 7) % C.skin.length],
  };
}

// === 高精細等距像素圖集快取（瀏覽器端非同步載入，支援 /pixel 與根路徑，含台灣／日系正背面人類與 24 種純天然犬貓圖集） ===
type AssetKey =
  | 'clean'
  | 'roomSpring'
  | 'roomSummer'
  | 'roomAutumn'
  | 'roomWinter'
  | 'vistaSkyline'
  | 'vistaSkylineNight'
  | 'vistaHarbor'
  | 'vistaAlpine'
  | 'vistaSakura'
  | 'humans'
  | 'humansBack'
  | 'humansSit'
  | 'humansTw'
  | 'humansTwBack'
  | 'humansTwSit'
  | 'dogs'
  | 'dogsBack'
  | 'dogsRest'
  | 'dogsStretch'
  | 'cats'
  | 'catsBack'
  | 'catsRest'
  | 'catsStretch';
const assetCache: Partial<Record<AssetKey, HTMLImageElement>> = {};
const assetReady: Partial<Record<AssetKey, boolean>> = {};
let assetsInitialized = false;

function ensureAssetsLoaded() {
  if (assetsInitialized || typeof window === 'undefined' || typeof Image === 'undefined') return;
  assetsInitialized = true;
  // server.py 將 web/pixel 掛載於 /pixel/* 與 /static/*（即使首頁路徑為 /）
  const files: Record<AssetKey, string> = {
    clean: `/pixel/pixel-assets/iso-room-clean.png`,
    roomSpring: `/pixel/pixel-assets/iso-room-spring.png`,
    roomSummer: `/pixel/pixel-assets/iso-room-summer.png`,
    roomAutumn: `/pixel/pixel-assets/iso-room-autumn.png`,
    roomWinter: `/pixel/pixel-assets/iso-room-winter.png`,
    vistaSkyline: `/pixel/pixel-assets/vista-skyline.png`,
    vistaSkylineNight: `/pixel/pixel-assets/vista-skyline-night.png`,
    vistaHarbor: `/pixel/pixel-assets/vista-harbor.png`,
    vistaAlpine: `/pixel/pixel-assets/vista-alpine.png`,
    vistaSakura: `/pixel/pixel-assets/vista-sakura.png`,
    humans: `/pixel/pixel-assets/roster-humans-12.png`,
    humansBack: `/pixel/pixel-assets/roster-humans-back-12.png`,
    humansSit: `/pixel/pixel-assets/roster-humans-sit-12.png`,
    humansTw: `/pixel/pixel-assets/roster-humans-tw-12.png`,
    humansTwBack: `/pixel/pixel-assets/roster-humans-tw-back-12.png`,
    humansTwSit: `/pixel/pixel-assets/roster-humans-tw-sit-12.png`,
    dogs: `/pixel/pixel-assets/roster-dogs-12.png`,
    dogsBack: `/pixel/pixel-assets/roster-dogs-back-12.png`,
    dogsRest: `/pixel/pixel-assets/roster-dogs-rest-12.png`,
    dogsStretch: `/pixel/pixel-assets/roster-dogs-stretch-12.png`,
    cats: `/pixel/pixel-assets/roster-cats-12.png`,
    catsBack: `/pixel/pixel-assets/roster-cats-back-12.png`,
    catsRest: `/pixel/pixel-assets/roster-cats-rest-12.png`,
    catsStretch: `/pixel/pixel-assets/roster-cats-stretch-12.png`,
  };
  (Object.keys(files) as AssetKey[]).forEach((k) => {
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => {
      assetReady[k] = true;
    };
    img.onerror = () => {
      // Fallback for `next dev` where public/ is served at `/pixel-assets/...`
      const fallbackImg = new Image();
      fallbackImg.onload = () => {
        assetCache[k] = fallbackImg;
        assetReady[k] = true;
      };
      fallbackImg.src = files[k].replace(/^\/pixel/, '');
    };
    img.src = files[k];
    assetCache[k] = img;
  });
}

interface SpriteCellMetrics {
  /** Normalized opaque bottom line (0..1) inside 240x240 cell so feet lock exactly to contact shadow */
  footRatio: number;
  /** Normalized opaque horizontal span (0..1) for shadow width calibration */
  widthRatio: number;
  /** Normalized leg-zone split line (0..1) separating upper torso from articulating legs */
  legSplitRatio: number;
}

const spriteMetricsCache = new Map<string, SpriteCellMetrics>();

function getSpriteCellMetrics(sheetKey: AssetKey, img: HTMLImageElement, cellIndex: number, isAnimal: boolean): SpriteCellMetrics {
  const cacheKey = `${sheetKey}:${cellIndex}`;
  const cached = spriteMetricsCache.get(cacheKey);
  if (cached) return cached;

  const fallback: SpriteCellMetrics = {
    footRatio: isAnimal ? 226 / 240 : 224 / 240,
    widthRatio: isAnimal ? 0.72 : 0.44,
    legSplitRatio: isAnimal ? 0.72 : 0.76,
  };

  if (typeof document === 'undefined' || !img.naturalWidth || !img.naturalHeight) {
    return fallback;
  }

  try {
    const col = cellIndex % 4;
    const row = Math.floor(cellIndex / 4);
    const cellW = Math.floor(img.naturalWidth / 4);
    const cellH = Math.floor(img.naturalHeight / 3);
    const canvas = document.createElement('canvas');
    canvas.width = cellW;
    canvas.height = cellH;
    const c = canvas.getContext('2d');
    if (!c) return fallback;
    c.drawImage(img, col * cellW, row * cellH, cellW, cellH, 0, 0, cellW, cellH);
    const data = c.getImageData(0, 0, cellW, cellH).data;

    let minX = cellW;
    let maxX = 0;
    let minY = cellH;
    let maxY = 0;
    for (let y = 0; y < cellH; y += 2) {
      for (let x = 0; x < cellW; x += 2) {
        if (data[(y * cellW + x) * 4 + 3] > 24) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }
    }
    if (maxY <= minY) {
      spriteMetricsCache.set(cacheKey, fallback);
      return fallback;
    }
    const contentH = maxY - minY;
    const metrics: SpriteCellMetrics = {
      footRatio: Math.min(1, (maxY + 1) / cellH),
      widthRatio: Math.max(0.28, Math.min(0.92, (maxX - minX) / cellW)),
      legSplitRatio: Math.min(0.9, (minY + contentH * (isAnimal ? 0.71 : 0.76)) / cellH),
    };
    spriteMetricsCache.set(cacheKey, metrics);
    return metrics;
  } catch {
    spriteMetricsCache.set(cacheKey, fallback);
    return fallback;
  }
}

// === 45° 等距無前牆向量回退繪圖（在 Node 測試或圖片載入前提供零白屏 45° 畫面） ===
const OX = 120;
const OY = 16;
const TW = 6.2;
const TH = 3.1;

function isoPt(u: number, v: number, z = 0): { x: number; y: number } {
  return {
    x: OX + (u - v) * TW,
    y: OY + (u + v) * TH - z,
  };
}

function drawIsoPoly(ctx: CanvasRenderingContext2D, pts: { x: number; y: number }[], fill: string, stroke?: string) {
  if (typeof ctx.beginPath !== 'function') {
    px(ctx, pts[0].x, pts[0].y, 8, 4, fill);
    return;
  }
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
  ctx.closePath();
  ctx.fill();
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 0.6;
    ctx.stroke();
  }
}

function drawIsoBoxFallback(
  ctx: CanvasRenderingContext2D,
  u: number,
  v: number,
  wU: number,
  wV: number,
  hZ: number,
  topCol: string,
  leftCol: string,
  rightCol: string
) {
  const t0 = isoPt(u, v, hZ);
  const t1 = isoPt(u + wU, v, hZ);
  const t2 = isoPt(u + wU, v + wV, hZ);
  const t3 = isoPt(u, v + wV, hZ);
  const b1 = isoPt(u + wU, v, 0);
  const b2 = isoPt(u + wU, v + wV, 0);
  const b3 = isoPt(u, v + wV, 0);
  drawIsoPoly(ctx, [t3, t2, b2, b3], leftCol, '#241710');
  drawIsoPoly(ctx, [t1, t2, b2, b1], rightCol, '#241710');
  drawIsoPoly(ctx, [t0, t1, t2, t3], topCol, '#241710');
}

export function paintBackground(ctx: CanvasRenderingContext2D, _id: string, night: boolean) {
  px(ctx, 0, 0, LW, LH, night ? '#1f1712' : '#c6a477');
  const U = 13;
  const V = 13;
  // 45° 去前牆木造地基斷面
  drawIsoBoxFallback(ctx, 0, 0, U, V, 4, '#9c663b', '#3d2310', '#2b180b');
  // 杉木地板與草編榻榻米
  for (let u = 0; u < U; u++) {
    for (let v = 0; v < V; v++) {
      const tatami = u >= 5 && v <= 9;
      drawIsoPoly(
        ctx,
        [isoPt(u, v, 4), isoPt(u + 1, v, 4), isoPt(u + 1, v + 1, 4), isoPt(u, v + 1, 4)],
        tatami ? ((u + v) % 2 ? '#98ad72' : '#8ca165') : (u + v) % 2 ? '#a86b3e' : '#965d34',
        tatami ? '#5c6e3f' : '#6e4323'
      );
    }
  }
  // 後方左牆與右牆（近側兩面牆完全拿掉！）
  drawIsoBoxFallback(ctx, 0, 0, 0.8, V, 32, '#5c381e', '#784e2f', '#8c5a36');
  drawIsoBoxFallback(ctx, 0, 0, U, 0.8, 32, '#5c381e', '#966540', '#784e2f');
  // 圍爐裏（地爐）與兩張雙螢幕木桌
  drawIsoBoxFallback(ctx, 7.5, 6.5, 3.2, 3.2, 7, '#6e472b', '#52341e', '#3d2514');
  drawIsoBoxFallback(ctx, 2.2, 5.5, 2.6, 3.6, 9, '#b87a44', '#8c5830', '#6e4323');
  drawIsoBoxFallback(ctx, 2.2, 1.8, 2.6, 3.4, 9, '#b87a44', '#8c5830', '#6e4323');
}

// === 等距透視雙螢幕動態光效（精準裁切於 4 台黑色螢幕面板玻璃內，絕不超出螢幕外框！） ===
interface MonitorScreenSpec {
  poly: [number, number][]; // 螢幕內框四角（邏輯座標 240x135）
  x0: number;
  y0: number;
  width: number;
  slope: number; // 螢幕上緣斜率 dy/dx
}

const MONITOR_SPECS: [MonitorScreenSpec, MonitorScreenSpec, MonitorScreenSpec, MonitorScreenSpec] = [
  // Monitor 1: 左前桌左螢幕（斜率 -0.52）
  {
    poly: [
      [74.2, 62.6],
      [83.3, 57.8],
      [83.3, 66.0],
      [74.2, 70.8],
    ],
    x0: 74.8,
    y0: 63.2,
    width: 7.8,
    slope: -0.52,
  },
  // Monitor 2: 左前桌右螢幕（微曲面斜率 -0.18）
  {
    poly: [
      [86.0, 56.4],
      [96.6, 54.4],
      [96.6, 62.0],
      [86.0, 64.0],
    ],
    x0: 86.6,
    y0: 57.2,
    width: 9.2,
    slope: -0.18,
  },
  // Monitor 3: 右後桌左螢幕（斜率 -0.53）
  {
    poly: [
      [101.0, 50.6],
      [109.3, 46.0],
      [109.3, 53.6],
      [101.0, 58.2],
    ],
    x0: 101.6,
    y0: 51.2,
    width: 7.0,
    slope: -0.53,
  },
  // Monitor 4: 右後桌右螢幕（微曲面斜率 -0.18）
  {
    poly: [
      [112.0, 44.6],
      [122.6, 42.6],
      [122.6, 50.0],
      [112.0, 52.0],
    ],
    x0: 112.6,
    y0: 45.4,
    width: 9.2,
    slope: -0.18,
  },
];

function drawClippedMonitorScreen(
  ctx: CanvasRenderingContext2D,
  spec: MonitorScreenSpec,
  kind: string,
  t: number
) {
  if (!kind || kind === 'off') return;
  const canClip = typeof ctx.save === 'function' && typeof ctx.beginPath === 'function' && typeof ctx.clip === 'function';
  if (canClip) {
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(spec.poly[0][0], spec.poly[0][1]);
    for (let i = 1; i < spec.poly.length; i++) ctx.lineTo(spec.poly[i][0], spec.poly[i][1]);
    ctx.closePath();
    ctx.clip();
  }

  const cx = spec.x0 + spec.width * 0.48;
  const cy = spec.y0 + spec.width * 0.48 * spec.slope + 3.0;

  if (kind === 'err' || kind === 'wait') {
    const col = kind === 'err' ? '#ef4444' : '#fbbf24';
    fpx(ctx, cx - 2.2, cy - 2.0, 4.4, 4.0, kind === 'err' ? 'rgba(239,68,68,0.28)' : 'rgba(251,191,36,0.28)');
    fpx(ctx, cx - 0.5, cy - 1.8, 1.0, 2.0, col);
    fpx(ctx, cx - 0.5, cy + 0.8, 1.0, 0.8, col);
    if (canClip) ctx.restore();
    return;
  }

  if (kind === 'ok') {
    fpx(ctx, cx - 2.2, cy - 1.5, 4.4, 3.2, 'rgba(34,197,94,0.22)');
    fpx(ctx, cx - 1.2, cy + 0.2, 0.8, 0.8, '#4ade80');
    fpx(ctx, cx - 0.4, cy + 0.8, 0.8, 0.8, '#4ade80');
    fpx(ctx, cx + 0.4, cy, 0.8, 0.8, '#4ade80');
    fpx(ctx, cx + 1.2, cy - 0.8, 0.8, 0.8, '#4ade80');
    if (canClip) ctx.restore();
    return;
  }

  const scroll = Math.floor(t / 190);
  const palette =
    kind === 'diff'
      ? ['#f87171', '#4ade80', '#94a3b8', '#4ade80']
      : kind === 'doc'
        ? ['#93c5fd', '#e2e8f0', '#fcd34d']
        : ['#34d399', '#38bdf8', '#fbbf24', '#c084fc'];

  // 嚴格沿著每台螢幕面板的透視斜率（spec.slope）在螢幕內部繪製 4 行程式碼橫條
  for (let row = 0; row < 4; row++) {
    const col = palette[(row + scroll) % palette.length];
    const indent = ((row + scroll) % 2) * 0.8;
    const barSteps = 3 + (((row * 5 + scroll) * 3) % 4);
    const startX = spec.x0 + 0.6 + indent;
    const startY = spec.y0 + 0.7 + row * 1.45 + indent * spec.slope;
    for (let s = 0; s < barSteps; s++) {
      const dx = s * 1.05;
      if (indent + dx + 0.9 <= spec.width - 0.5) {
        fpx(ctx, startX + dx, startY + dx * spec.slope, 0.85, 0.55, col);
      }
    }
  }

  if (canClip) ctx.restore();
}

function drawScreens(ctx: CanvasRenderingContext2D, sc: { impl?: string; rev?: string; test?: string }, t: number) {
  // 左前雙螢幕桌（Implementer 座位：Monitor 1 & Monitor 2）
  drawClippedMonitorScreen(ctx, MONITOR_SPECS[0], sc.impl || 'code', t);
  drawClippedMonitorScreen(ctx, MONITOR_SPECS[1], sc.impl === 'code' ? 'doc' : sc.impl || 'code', t + 500);
  // 右後雙螢幕桌（Reviewer 座位：Monitor 3 & Monitor 4）
  drawClippedMonitorScreen(ctx, MONITOR_SPECS[2], sc.rev || 'diff', t + 300);
  drawClippedMonitorScreen(ctx, MONITOR_SPECS[3], sc.rev || 'code', t + 900);
}

// === 後方右牆敏捷看板（保留底圖高精細像素便利貼，不疊加粗糙方塊） ===
function drawBoard(_ctx: CanvasRenderingContext2D, _board: { crit: string[]; commits: number; writing: boolean } | null, _t: number) {
  // 底圖 iso-room-clean.png 已內建高精細像素便利貼與看板細節，不額外繪製低解析色塊
}

// === 復古掛鐘即時指針 ===
function drawClock(ctx: CanvasRenderingContext2D, now: number) {
  const d = new Date(now);
  const cx = 164.5;
  const cy = 37.5;
  const hAng = (((d.getHours() % 12) + d.getMinutes() / 60) / 12) * Math.PI * 2;
  const mAng = (d.getMinutes() / 60) * Math.PI * 2;
  for (let i = 1; i <= 3; i++) {
    fpx(ctx, cx + Math.sin(mAng) * i * 0.75, cy - Math.cos(mAng) * i * 0.75, 0.5, 0.5, '#241710');
  }
  for (let i = 1; i <= 2; i++) {
    fpx(ctx, cx + Math.sin(hAng) * i * 0.75, cy - Math.cos(hAng) * i * 0.75, 0.6, 0.6, '#b91c1c');
  }
  fpx(ctx, cx - 0.25, cy - 0.25, 0.6, 0.6, '#241710');
}

// 窗戶開口精確裁切多邊形（貼合木樑下緣、左右障子窗框、以及前方雙螢幕上緣）
const WINDOW_OPENING_POLY: [number, number][] = [
  [66.0, 44.5],
  [99.0, 26.8],
  [99.0, 55.2],
  [84.5, 55.2],
  [72.5, 62.5],
  [72.5, 70.0],
  [66.0, 73.5],
];

/**
 * 繪製 16 種房間專屬窗外四季光影與細膩天氣粒子：
 * - 100% 保留底圖 iso-room-clean.png 窗外原有的高精細像素山林與樹葉層次（絕不使用粗糙色塊覆蓋！）
 * - 透過 Canvas 色彩混合模式（color / soft-light / multiply）在窗戶多邊形內渲染春櫻、夏綠、秋楓、冬雪、夕照、星夜氛圍光
 * - 疊加極細膩的 0.45 像素級微粒（飄落櫻瓣、細雨絲、瑞雪結晶、夏夜螢火）
 */
function drawWindowSceneryAndWeather(
  ctx: CanvasRenderingContext2D,
  theme: RoomThemeSpec,
  roomId: string,
  t: number
) {
  const canClip = typeof ctx.save === 'function' && typeof ctx.beginPath === 'function' && typeof ctx.clip === 'function';
  if (!canClip) return;

  ctx.save();
  ctx.beginPath();
  ctx.moveTo(WINDOW_OPENING_POLY[0][0], WINDOW_OPENING_POLY[0][1]);
  for (let i = 1; i < WINDOW_OPENING_POLY.length; i++) {
    ctx.lineTo(WINDOW_OPENING_POLY[i][0], WINDOW_OPENING_POLY[i][1]);
  }
  ctx.closePath();
  ctx.clip();

  // 1. 以色彩混合（Color / Soft-Light）保留底圖高精細樹葉紋理並轉換為 16 種季節色調
  if (typeof ctx.createLinearGradient === 'function') {
    const skyGrad = ctx.createLinearGradient(82, 26, 82, 72);
    skyGrad.addColorStop(0, theme.skyTop);
    skyGrad.addColorStop(0.55, theme.foliagePrimary);
    skyGrad.addColorStop(1, theme.foliageSecondary);

    ctx.save();
    ctx.globalCompositeOperation =
      theme.weather === 'starry' || theme.weather === 'thunder' || theme.weather === 'blizzard'
        ? 'multiply'
        : 'color';
    ctx.globalAlpha =
      theme.weather === 'starry' || theme.weather === 'thunder'
        ? 0.62
        : theme.season === 'winter'
          ? 0.52
          : 0.42;
    ctx.fillStyle = skyGrad;
    ctx.fillRect(65, 25, 36, 50);
    ctx.restore();

    // 冬季瑞雪覆蓋樹梢柔光
    if (theme.season === 'winter' && theme.weather !== 'starry') {
      ctx.save();
      ctx.globalCompositeOperation = 'screen';
      ctx.globalAlpha = 0.28;
      ctx.fillStyle = '#e0f2fe';
      ctx.fillRect(65, 25, 36, 50);
      ctx.restore();
    }
  }

  // 2. 極細緻天氣微粒（嚴格限制於 0.35~0.55 邏輯單位，絕不產生方塊感）
  const seedBase = hash(roomId);
  if (theme.weather === 'rain' || theme.weather === 'thunder') {
    ctx.strokeStyle = 'rgba(224, 242, 254, 0.62)';
    ctx.lineWidth = 0.3;
    for (let r = 0; r < 14; r++) {
      const prog = ((t / 24 + r * 11 + seedBase) % 32) / 32;
      const rx = 68 + ((r * 7) % 28) - prog * 4;
      const ry = 28 + prog * 38;
      ctx.beginPath();
      ctx.moveTo(rx, ry);
      ctx.lineTo(rx - 0.7, ry + 2.4);
      ctx.stroke();
    }
    if (theme.weather === 'thunder' && t % 4200 < 160) {
      ctx.fillStyle = 'rgba(254, 249, 195, 0.38)';
      ctx.fillRect(65, 25, 36, 50);
    }
  } else if (theme.weather === 'snow' || theme.weather === 'blizzard' || theme.weather === 'onsen') {
    const windSpeed = theme.weather === 'blizzard' ? 22 : 55;
    const windDrift = theme.weather === 'blizzard' ? -7 : -1.8;
    ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
    for (let s = 0; s < 12; s++) {
      const prog = ((t / windSpeed + s * 9 + seedBase) % 30) / 30;
      const sx = 70 + ((s * 6) % 27) + prog * windDrift + Math.sin(t / 320 + s) * 0.9;
      const sy = 28 + prog * 36;
      ctx.beginPath();
      ctx.arc(sx, sy, 0.42, 0, Math.PI * 2);
      ctx.fill();
    }
  } else if (theme.weather === 'firefly' || theme.weather === 'starry') {
    for (let f = 0; f < 8; f++) {
      const fx = 69 + ((f * 5) % 26) + Math.sin(t / 480 + f * 2) * 1.8;
      const fy = 33 + ((f * 6) % 26) + Math.cos(t / 400 + f) * 1.5;
      const glow = (Math.sin(t / 220 + f * 1.7) + 1) * 0.42;
      ctx.fillStyle =
        theme.weather === 'firefly'
          ? `rgba(190, 242, 100, ${glow.toFixed(2)})`
          : `rgba(254, 240, 138, ${glow.toFixed(2)})`;
      ctx.beginPath();
      ctx.arc(fx, fy, 0.45, 0, Math.PI * 2);
      ctx.fill();
    }
  } else {
    // 細緻飄落花瓣／紅葉微粒（圓潤小花瓣，無方塊感）
    const petalCols =
      theme.weather === 'sakura'
        ? ['#fbcfe8', '#f472b6', '#ffe4e6']
        : theme.weather === 'ginkgo'
          ? ['#fde047', '#facc15', '#fef08a']
          : theme.season === 'autumn'
            ? ['#f87171', '#fb923c', '#fbbf24']
            : ['#a7f3d0', '#fef08a', '#ffffff'];
    for (let i = 0; i < 8; i++) {
      const cycle = (t / 58 + i * 17 + (seedBase % 37)) % 34;
      const lx = 68 + ((i * 7 + cycle * 0.45) % 28) + Math.sin(t / 360 + i) * 0.8;
      const ly = 29 + (cycle % 30);
      ctx.fillStyle = petalCols[(i + seedBase) % petalCols.length];
      ctx.beginPath();
      ctx.arc(lx, ly, 0.42, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  ctx.restore();
}

/**
 * 依據房間主題渲染圍爐裏柔和炭火暖光（保留底圖 iso-room-clean.png 的高精細坐墊、地爐炭火與手沖咖啡器皿，絕不繪製粗糙方塊覆蓋！）
 */
function drawIndoorThemeDecor(ctx: CanvasRenderingContext2D, theme: RoomThemeSpec, t: number) {
  const hx = 138;
  const hy = 90;
  const flicker = Math.sin(t / 180) * 1.2;
  if (typeof ctx.createRadialGradient === 'function') {
    const radius = theme.season === 'winter' ? 30 : 24;
    const grad = ctx.createRadialGradient(hx, hy - 2, 1.2, hx, hy - 2, radius + flicker);
    grad.addColorStop(0, 'rgba(255, 180, 75, 0.34)');
    grad.addColorStop(0.55, 'rgba(255, 135, 40, 0.12)');
    grad.addColorStop(1, 'rgba(255, 110, 20, 0)');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(hx, hy - 2, radius + 2, 0, Math.PI * 2);
    ctx.fill();
  }
}

function mark(ctx: CanvasRenderingContext2D, x: number, y: number, kind: string, t: number) {
  const up = Math.floor(t / 280) % 2 ? -1.25 : 0;
  const fill = kind === 'err' ? C.red : C.heat;
  const bx = Math.round(x) - 3.5;
  const by = Math.round(y) - 10 + up;
  fpx(ctx, bx - 0.75, by - 0.75, 7.5, 8.5, C.dark);
  fpx(ctx, bx, by, 6, 7, fill);
  fpx(ctx, bx + 2, by + 1, 2, 3, '#ffffff');
  fpx(ctx, bx + 2, by + 5, 2, 1, '#ffffff');
}

function roomBang(ctx: CanvasRenderingContext2D, t: number) {
  const up = Math.floor(t / 360) % 2 ? -1 : 0;
  fpx(ctx, LW - 22, 6 + up, 12, 15, C.dark);
  fpx(ctx, LW - 21, 7 + up, 10, 13, C.heat);
  fpx(ctx, LW - 17, 9 + up, 2, 6, '#ffffff');
  fpx(ctx, LW - 17, 16 + up, 2, 2, '#ffffff');
}

export type PersonaSlotKey = 'supervisor' | 'implementer' | 'reviewer';

export interface RoomPersona {
  id: PersonaId;
  slotKey: PersonaSlotKey;
  tier: PersonaTier;
  tierLabel: string;
  roleTitle: string;
  engineLabel: string;
  badgeText: string;
  badgeColor: string;
  gender: 'M' | 'F';
  x: number;
  y: number;
  vx?: number;
  vy?: number;
  pose: string;
  posturePhase?: HumanPosturePhase;
  seatBlend?: number;
  useBack?: boolean;
  facing?: 'left' | 'right' | 'front';
  shirt: string;
  look: { hair: string; style: string; skin: string };
  character: CharacterProfile;
  mark?: string;
  bubble: string;
  bubbleTone?: 'wait' | 'err' | 'ok' | 'think' | '';
  isFallback?: boolean;
  isWorking?: boolean;
  recentRecords: TeamEvent[];
}

export function sortEventsDesc(events: TeamEvent[]): TeamEvent[] {
  const seen = new Set<string>();
  const out: TeamEvent[] = [];
  const indexed = events.map((ev, idx) => ({ ev, idx }));
  indexed.sort((a, b) => {
    const ta = a.ev.ts || '';
    const tb = b.ev.ts || '';
    if (ta !== tb) return tb.localeCompare(ta);
    return b.idx - a.idx;
  });
  for (const { ev } of indexed) {
    const key = ev.id || `${ev.ts}:${ev.kind}:${ev.text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(ev);
  }
  return out;
}

export function getPersonaRecentRecords(
  room: TeamRoom | null | undefined,
  extraEvents: TeamEvent[] | undefined,
  slotKey: PersonaSlotKey,
  limit = 5
): TeamEvent[] {
  const combined = [...(extraEvents || []), ...(room?.events || [])];
  const desc = sortEventsDesc(combined);
  const matched = desc.filter((e) => {
    const role = (e.role || '').toLowerCase();
    const kind = (e.kind || '').toLowerCase();
    const txt = e.text || '';
    if (slotKey === 'supervisor') {
      if (['po', 'tech_lead', 'scrum', 'qa', 'delegate'].includes(role)) return true;
      if (['phase', 'spec', 'decision', 'delegate', 'assign', 'inbox', 'status', 'user'].includes(kind)) return true;
      if (/^(【(PO|Tech Lead|Scrum|QA|你)】|D1 權限把關|自動允許|代管決定|已跟隨 CE 計畫)/.test(txt)) return true;
      return false;
    }
    if (slotKey === 'implementer') {
      if (['implementer', 'fixer', 'impl'].includes(role)) return true;
      if (kind === 'prefs' && role === 'impl') return true;
      if (kind === 'worktree') return true;
      if (/^【(實作|修復)】|實作|修改|File |git |pytest|編輯|執行/.test(txt)) return true;
      if (['tool', 'tool_result', 'agent', 'prompt', 'cli_usage'].includes(kind) && role !== 'reviewer' && role !== 'review') {
        return true;
      }
      if (kind === 'say' && !role && !/^【(PO|Tech Lead|Scrum|QA|審查)】/.test(txt)) return true;
      return false;
    }
    if (slotKey === 'reviewer') {
      if (['reviewer', 'review'].includes(role)) return true;
      if (kind === 'prefs' && role === 'review') return true;
      if (/^【審查】/.test(txt) || /審查/.test(txt)) return true;
      return false;
    }
    return false;
  });
  return matched.slice(0, limit);
}

export function findActiveCriterionIndex(
  criteria: TeamCriterion[] | undefined,
  room?: TeamRoom | null,
  recentEvents?: TeamEvent[]
): number {
  const list = Array.isArray(criteria) ? criteria : [];
  if (list.length === 0) return -1;

  const explicitIdx = list.findIndex((c) => c.status === 'running' || c.status === 'in_progress');
  if (explicitIdx >= 0) return explicitIdx;

  const desc = sortEventsDesc([...(recentEvents || []), ...(room?.events || [])]).slice(0, 6);
  const haystacks = [room?.activity || '', ...desc.map((e) => e.text || ''), room?.goal || ''].filter(Boolean);

  for (const hay of haystacks) {
    for (let i = 0; i < list.length; i++) {
      const c = list[i];
      if (c.done || c.status === 'verified') continue;
      const cid = (c.id || '').trim();
      if (cid && new RegExp(`\\b${cid.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(hay)) {
        return i;
      }
    }
  }

  return list.findIndex((c) => !c.done && c.status !== 'verified');
}

/**
 * 繪製辦公椅背與左扶手前景遮罩（當角色坐在雙螢幕桌前時，讓角色背部自然嵌進辦公椅內！）
 */
function drawChairBackrestOverlay(ctx: CanvasRenderingContext2D, seat: 'impl' | 'rev') {
  const cleanImg = assetCache.clean;
  if (!assetReady.clean || !cleanImg || typeof ctx.save !== 'function' || typeof ctx.clip !== 'function') return;
  ctx.save();
  ctx.beginPath();
  if (seat === 'impl') {
    // 左前辦公椅（implSeat）椅背與左扶手多邊形（邏輯座標 240x135）
    const pts = [
      [399 / 4, 304 / 4],
      [424 / 4, 294 / 4],
      [426 / 4, 338 / 4],
      [370 / 4, 338 / 4],
      [370 / 4, 314 / 4],
      [396 / 4, 322 / 4],
    ];
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  } else {
    // 右後辦公椅（revSeat）椅背與左扶手多邊形（邏輯座標 240x135）
    const pts = [
      [500 / 4, 250 / 4],
      [525 / 4, 240 / 4],
      [527 / 4, 286 / 4],
      [471 / 4, 286 / 4],
      [471 / 4, 262 / 4],
      [497 / 4, 270 / 4],
    ];
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  }
  ctx.closePath();
  ctx.clip();
  ctx.drawImage(cleanImg, 0, 0, LW, LH);
  ctx.restore();
}

interface SpriteDrawPhysicsOpts {
  solar?: SolarLighting;
  gait?: QuadrupedGaitState;
  facingLeft?: boolean;
  seatBlend?: number;
  useBackHint?: boolean;
  behavior?: string;
}

/**
 * 繪製 48 角色庫中的任一高精細角色（24 位台灣／日系人類 ＋ 24 種純天然四足犬貓）：
 * - 自動量測每格精靈圖實際腳底線 (footRatio)，讓接觸陰影 100% 鎖在腳掌下（絕不浮空）並疊加 SunCalc 太陽投射斜影
 * - 四足犬貓採用腳步驅動身體（Foot-Driven Quadruped Gait）：前後肢獨立在肩／髖樞紐跨步與抬腳，軀幹與頭部平穩滑行不抖動
 * - 趴姿／坐姿寵物嚴格零位移；站姿寵物支援 趴臥 → 起身 → 伸懶腰 → 四足邁步 的自然動作過渡
 * - 人類坐座墊採用柔和坐姿壓縮與座墊凹陷陰影（AO），徹底根除「插進坐墊」的生硬水平切口
 */
function drawCharacterSprite(
  ctx: CanvasRenderingContext2D,
  char: CharacterProfile,
  cx: number,
  cy: number,
  pose: string,
  t: number,
  isSelected: boolean,
  scaleMult = 1.0,
  vx = 0,
  vy = 0,
  interactionHeart = false,
  opts?: SpriteDrawPhysicsOpts
) {
  const isAnimal = char.species === 'animal';
  const effectiveScale = (char.sizeScale || (isAnimal ? 0.78 : 1.0)) * scaleMult;
  const baseSize = 23.0 * effectiveScale;

  const isDeskSit = pose === 'type' || pose === 'sit' || pose === 'head';
  const isCushionSit = pose === 'sofa' || pose === 'mug' || pose === 'cheer' || pose === 'petting';
  const isBoardWork = pose === 'write';
  const isWalking = pose === 'walk' || pose === 'trot' || pose === 'dash';
  const solar = opts?.solar;
  const gait = opts?.gait;
  const seatBlend = opts?.seatBlend ?? (isDeskSit || isCushionSit ? 1 : 0);

  const walkBoardX = isBoardWork ? Math.sin(t / 760) * 1.1 : 0;
  const walkBoardY = isBoardWork ? Math.sin(t / 760) * 0.55 : 0;
  const pxX = cx + walkBoardX;
  const pxY = cy + walkBoardY;

  // === 分支 A：純天然四足犬貓 ===
  if (isAnimal) {
    const basePetKey: AssetKey = char.sheet === 'cats' ? 'cats' : 'dogs';
    const nativePosture = getPetNativePosture(char.id);
    const behavior = opts?.behavior;
    const speed = Math.hypot(vx, vy);
    const isMovingState = behavior === 'walking' || behavior === 'trotting' || behavior === 'chase_ball';
    const walkingUpward =
      isMovingState &&
      speed >= 0.25 &&
      (opts?.useBackHint !== undefined ? opts.useBackHint : vy < -0.16);
    // 依真實行為與移動方向自動切換專屬像素圖集（趴臥 / 伸懶腰 / 背面向上走 / 正側面走動），100% 完整不斷腿！
    let petKey: AssetKey = basePetKey;
    if (behavior === 'loaf') {
      const restKey: AssetKey | null = basePetKey === 'cats' ? 'catsRest' : basePetKey === 'dogs' ? 'dogsRest' : null;
      if (restKey && assetReady[restKey]) petKey = restKey;
    } else if (behavior === 'stretching') {
      const stretchKey: AssetKey | null =
        basePetKey === 'cats' ? 'catsStretch' : basePetKey === 'dogs' ? 'dogsStretch' : null;
      if (stretchKey && assetReady[stretchKey]) petKey = stretchKey;
    } else if (walkingUpward) {
      const backKey: AssetKey | null = basePetKey === 'cats' ? 'catsBack' : basePetKey === 'dogs' ? 'dogsBack' : null;
      if (backKey && assetReady[backKey]) petKey = backKey;
    }
    const petImg = assetCache[petKey];
    const facingLeft = opts?.facingLeft !== undefined ? opts.facingLeft : vx > 0.04 ? false : vx < -0.04 ? true : pxX >= 115;
    const flipPetH = !facingLeft; // 原生圖集（含背面圖）皆面朝左；面朝右時水平翻轉
    const isWalkSheet = petKey === basePetKey || petKey === 'dogsBack' || petKey === 'catsBack';
    const activelyMoving = isWalkSheet && isMovingState && speed >= 0.25;

    if (assetReady[petKey] && petImg && typeof ctx.drawImage === 'function') {
      const col = char.cellIndex % 4;
      const row = Math.floor(char.cellIndex / 4);
      const cellW = 240;
      const cellH = 240;
      const sx0 = col * cellW;
      const sy0 = row * cellH;
      const metrics = getSpriteCellMetrics(petKey, petImg, char.cellIndex, true);

      const drawW = baseSize * 1.12;
      const drawH = baseSize * 1.12;
      // 精準把圖片實際腳底線 (footRatio) 對齊至 pxY，100% 消除浮空！
      const footOffset = drawH * metrics.footRatio;
      const shadowRx = drawW * metrics.widthRatio * 0.44;
      const shadowRy = shadowRx * 0.34;
      const bodyLift = activelyMoving && gait ? gait.bodyLift : 0;
      const tuckAmt = gait ? gait.tuckAmt : 0;
      const stretchAmt = gait ? gait.stretchAmt : 0;

      // 1. 貼地等距接觸陰影 ＋ SunCalc 動態太陽斜影（與腳掌位置、跳躍高度嚴格連動）
      if (typeof ctx.beginPath === 'function' && typeof ctx.ellipse === 'function') {
        ctx.save();
        const isoTilt = facingLeft ? -0.18 : 0.18;
        // Solar cast shadow
        if (solar && solar.shadowAlpha > 0.05) {
          ctx.fillStyle = `rgba(24, 15, 9, ${(solar.shadowAlpha * 0.55).toFixed(2)})`;
          ctx.beginPath();
          ctx.ellipse(
            pxX + solar.shadowDx * 0.65,
            pxY - 0.5 + solar.shadowDy * 0.65,
            shadowRx * 1.05,
            shadowRy * 0.95,
            isoTilt,
            0,
            Math.PI * 2
          );
          ctx.fill();
        }
        // Direct contact ambient occlusion directly beneath paws/belly
        const contactAlpha = Math.max(0.24, 0.44 - bodyLift * 0.18);
        ctx.fillStyle = `rgba(24, 15, 9, ${contactAlpha.toFixed(2)})`;
        ctx.beginPath();
        ctx.ellipse(
          pxX,
          pxY - 0.5,
          shadowRx * (1 + tuckAmt * 0.08 - bodyLift * 0.08),
          shadowRy * (1 + tuckAmt * 0.12),
          isoTilt,
          0,
          Math.PI * 2
        );
        ctx.fill();
        ctx.restore();
      }

      const canSave = typeof ctx.save === 'function' && typeof ctx.translate === 'function' && typeof ctx.scale === 'function';
      if (canSave) {
        ctx.save();
        ctx.translate(pxX, pxY - bodyLift);
        ctx.scale(flipPetH ? -1 : 1, 1);

        if (activelyMoving && gait) {
          // 100% 完整不斷腿的 16-bit 像素小跑律動：隨步態節奏輕快微躍＋自然俯仰＋落地彈性微縮，絕不切割四肢！
          const phase = gait.cycle * Math.PI * 2;
          const stepBounce = Math.abs(Math.sin(phase)); // 0..1 兩拍式輕快小步
          const hopY = -stepBounce * 0.85;
          const trotTilt = Math.sin(phase) * 0.032; // 輕微前後重心交替，呈現邁步推進感
          const stretchX = 1 + (stepBounce - 0.5) * 0.036;
          const squashY = 1 - (stepBounce - 0.5) * 0.032;
          ctx.translate(0, hopY);
          if (typeof ctx.rotate === 'function') ctx.rotate(trotTilt);
          ctx.scale(stretchX, squashY);
          ctx.drawImage(petImg, sx0, sy0, cellW, cellH, -drawW / 2, -footOffset, drawW, drawH);
        } else {
          // 靜止站立／趴臥／伸懶腰：完全不抖動、不動腳，僅保留極微自然呼吸起伏
          const breatheAmp = behavior === 'stretching' ? 0.01 : 0.006;
          const breatheY = 1 + Math.sin(t / 760) * breatheAmp;
          ctx.scale(1, breatheY);
          ctx.drawImage(petImg, sx0, sy0, cellW, cellH, -drawW / 2, -footOffset, drawW, drawH);
        }

        ctx.restore();
      } else {
        ctx.drawImage(petImg, sx0, sy0, cellW, cellH, pxX - drawW / 2, pxY - footOffset, drawW, drawH);
      }

      // 撒嬌互動小愛心 ♥
      if (interactionHeart && typeof ctx.fillText === 'function') {
        const floatY = Math.sin(t / 260) * 1.1;
        ctx.save();
        ctx.font = 'bold 4.5px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillStyle = '#fb7185';
        ctx.fillText('♥', pxX, pxY - drawH * 0.88 + floatY);
        ctx.restore();
      }
    } else {
      const pw = 10 * effectiveScale;
      const ph = 8 * effectiveScale;
      fpx(ctx, pxX - pw / 2, pxY - ph, pw, ph, '#241710');
      fpx(ctx, pxX - pw / 2 + 0.6, pxY - ph + 0.6, pw - 1.2, ph - 1.2, char.shirt);
    }
    return;
  }

  // === 分支 B：24 位人類工作角色 ===
  let useBackSheet = false;
  let flipH = false;

  if (isDeskSit) {
    useBackSheet = true;
    flipH = false;
  } else if (isBoardWork) {
    const lookBackAtBoard = (t % 6400) < 4800;
    useBackSheet = lookBackAtBoard;
    flipH = lookBackAtBoard;
  } else if (isWalking) {
    useBackSheet = opts?.useBackHint !== undefined ? opts.useBackHint : vy < -0.15;
    flipH = vx > 0.1;
  } else {
    useBackSheet = false;
    flipH = pxX < 130 ? Math.floor(t / 4800) % 3 === 0 : false;
  }

  const frontKey: AssetKey = char.sheet === 'humansTw' ? 'humansTw' : 'humans';
  const backKey: AssetKey = char.sheet === 'humansTw' ? 'humansTwBack' : 'humansBack';
  const sitKey: AssetKey = char.sheet === 'humansTw' ? 'humansTwSit' : 'humansSit';
  // 坐在榻榻米座墊上時切換為專屬盤腿／正坐像素精靈圖，站起走動或白板作業時則顯示完整站姿！
  const activeKey: AssetKey =
    useBackSheet && assetReady[backKey]
      ? backKey
      : isCushionSit && seatBlend > 0.35 && assetReady[sitKey]
        ? sitKey
        : frontKey;
  const sheetImg = assetCache[activeKey];

  // 腳下柔和接觸陰影 ＋ SunCalc 動態太陽斜影 ＋ 選取高亮光圈
  const shadowR = 5.2 * effectiveScale;
  if (typeof ctx.beginPath === 'function' && typeof ctx.ellipse === 'function') {
    ctx.save();
    if (isSelected) {
      const pulse = Math.sin(t / 180) * 0.55;
      ctx.fillStyle = 'rgba(251, 191, 36, 0.34)';
      ctx.beginPath();
      ctx.ellipse(pxX, pxY - 0.3, shadowR + 2.0 + pulse, 2.5, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#fbbf24';
      ctx.lineWidth = 0.85;
      ctx.beginPath();
      ctx.ellipse(pxX, pxY - 0.3, shadowR + 0.8, 1.8, 0, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      if (solar && solar.shadowAlpha > 0.05 && !isDeskSit) {
        ctx.fillStyle = `rgba(24, 15, 9, ${(solar.shadowAlpha * 0.5).toFixed(2)})`;
        ctx.beginPath();
        ctx.ellipse(
          pxX + solar.shadowDx * 0.7,
          pxY - 0.3 + solar.shadowDy * 0.7,
          shadowR * 0.95,
          1.7 * effectiveScale,
          0.18,
          0,
          Math.PI * 2
        );
        ctx.fill();
      }
      // 坐墊壓陷接觸陰影（較寬較柔和，呈現身體重量壓在座墊上的自然觸感）
      const aoRx = isCushionSit ? shadowR * 1.15 : shadowR * 0.88;
      const aoRy = isCushionSit ? 2.25 * effectiveScale : 1.55 * effectiveScale;
      ctx.fillStyle = isCushionSit ? 'rgba(24, 15, 9, 0.42)' : 'rgba(24, 15, 9, 0.36)';
      ctx.beginPath();
      ctx.ellipse(pxX, pxY - 0.25, aoRx, aoRy, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  } else {
    fpx(ctx, pxX - shadowR, pxY - 1.4, shadowR * 2, 2.2, isSelected ? '#fbbf24' : 'rgba(28, 17, 10, 0.36)');
  }

  if (assetReady[activeKey] && sheetImg && typeof ctx.drawImage === 'function') {
    const col = char.cellIndex % 4;
    const row = Math.floor(char.cellIndex / 4);
    const cellW = 240;
    const cellH = 240;
    const sx0 = col * cellW;
    const sy0 = row * cellH;
    const metrics = getSpriteCellMetrics(activeKey, sheetImg, char.cellIndex, false);

    const drawW = baseSize;
    const drawH = baseSize;
    const footOffset = drawH * metrics.footRatio;
    const legSplitPx = Math.round(cellH * metrics.legSplitRatio);
    const upperSrcH = legSplitPx;
    const lowerSrcH = Math.max(12, cellH - legSplitPx);
    const upperDrawH = drawH * (upperSrcH / cellH);
    const lowerDrawH = drawH * (lowerSrcH / cellH);

    let bodyDx = 0;
    let bodyDy = 0;
    let bodyScaleX = 1.0;
    let bodyScaleY = 1.0;
    let bodyRot = 0;

    const walkSpeed = Math.hypot(vx, vy);
    const walkPhase = t / 68;

    if (pose === 'type') {
      bodyDx = Math.sin(t / 340) * 0.38 * effectiveScale;
      bodyDy = Math.abs(Math.sin(t / 105)) * 0.32 * effectiveScale;
      bodyRot = Math.sin(t / 320) * 0.018;
    } else if (pose === 'sit' || pose === 'head') {
      bodyDx = pose === 'head' ? Math.sin(t / 95) * 0.65 * effectiveScale : Math.sin(t / 620) * 0.25 * effectiveScale;
      bodyScaleY = 1.0 + Math.sin(t / 460) * 0.014;
      bodyRot = pose === 'head' ? Math.sin(t / 95) * 0.032 : Math.sin(t / 620) * 0.014;
    } else if (isBoardWork) {
      bodyDy = -Math.abs(Math.sin(t / 170)) * 0.55 * effectiveScale;
      bodyRot = Math.sin(t / 280) * 0.022;
    } else if (isCushionSit) {
      if (pose === 'cheer') {
        bodyDy = -Math.abs(Math.sin(t / 160)) * 1.25 * effectiveScale;
      } else if (pose === 'petting') {
        bodyRot = Math.sin(t / 240) * 0.03;
        bodyDy = 0.35 * effectiveScale;
      } else {
        bodyScaleY = 1.0 + Math.sin(t / 520) * 0.014;
        bodyScaleX = 1.0 - Math.sin(t / 520) * 0.008;
      }
    } else if (isWalking && walkSpeed > 0.04) {
      // 自然二足步態：重心微幅起伏，頭部保持端正不斜切歪頭！
      bodyDy = -Math.abs(Math.sin(walkPhase)) * 0.65 * effectiveScale;
      bodyRot = Math.sin(walkPhase) * 0.02;
    } else {
      bodyScaleY = 1.0 + Math.sin(t / 480) * 0.014;
      bodyScaleX = 1.0 - Math.sin(t / 480) * 0.008;
    }

    const canSave = typeof ctx.save === 'function' && typeof ctx.translate === 'function' && typeof ctx.scale === 'function';
    if (canSave) {
      ctx.save();
      ctx.translate(pxX + bodyDx, pxY + bodyDy);
      ctx.scale(flipH ? -bodyScaleX : bodyScaleX, bodyScaleY);
      if (typeof ctx.rotate === 'function' && bodyRot !== 0) {
        ctx.rotate(bodyRot);
      }

      if (isWalking && walkSpeed >= 0.12) {
        // 乾淨可愛的像素步行：100% 完整精靈圖＋柔和步韻彈跳，絕不切割雙腿或歪頭
        ctx.drawImage(sheetImg, sx0, sy0, cellW, cellH, -drawW / 2, -footOffset, drawW, drawH);
      } else {
        // 完整無壓扁、無截斷的高精細像素人物（坐辦公椅時由椅背前景遮罩自然包覆；在座墊區自然貼合座墊陰影）
        const deskSinkY = isDeskSit ? 1.8 * effectiveScale * seatBlend : 0;
        ctx.drawImage(sheetImg, sx0, sy0, cellW, cellH, -drawW / 2, -footOffset + deskSinkY, drawW, drawH);
      }

      ctx.restore();
    } else {
      ctx.drawImage(sheetImg, sx0, sy0, cellW, cellH, pxX - drawW / 2, pxY - footOffset + bodyDy, drawW, drawH);
    }
    return;
  }

  // Fallback：當測試環境或圖片尚未載入完成時，繪製深暖棕描邊日系 Q 版角色
  const fw = 9 * effectiveScale;
  const fh = 16 * effectiveScale;
  const x = pxX - fw / 2;
  const y = pxY - fh;
  fpx(ctx, x, y, fw, fh * 0.58, '#241710');
  fpx(ctx, x + 0.8, y + fh * 0.52, fw - 1.6, fh * 0.42, '#241710');
  fpx(ctx, x + 1.2, y + fh * 0.55, fw - 2.4, fh * 0.32, char.shirt);
  fpx(ctx, x + 0.8, y + 1.2, fw - 1.6, fh * 0.4, char.skin);
  fpx(ctx, x + 0.8, y, fw - 1.6, fh * 0.16, char.hair);
}

interface ActorTransition {
  fromX: number;
  fromY: number;
  toX: number;
  toY: number;
  targetSpot: string;
  targetPose: string;
  startMs: number;
  durationMs: number;
}

const actorTransitions = new Map<string, ActorTransition>();

// 室內實體障礙物碰撞盒（左前雙螢幕桌、右後雙螢幕桌、中央炭火圍爐裏／被爐）
export const SOLID_OBSTACLES = [
  { id: 'left_desk', x0: 74, y0: 58, x1: 96, y1: 81 },
  { id: 'right_desk', x0: 100, y0: 45, x1: 122, y1: 68 },
  { id: 'irori_hearth', x0: 129, y0: 82, x1: 147, y1: 97 },
] as const;

function segmentHitsRect(
  ax: number,
  ay: number,
  bx: number,
  by: number,
  rx0: number,
  ry0: number,
  rx1: number,
  ry1: number
): boolean {
  for (let i = 1; i <= 14; i++) {
    const t = i / 15;
    const px = ax + (bx - ax) * t;
    const py = ay + (by - ay) * t;
    if (px > rx0 && px < rx1 && py > ry0 && py < ry1) return true;
  }
  return false;
}

/**
 * 符合物理空間與家具碰撞的等距多節點尋路（Multi-Waypoint Isometric Nav-Path）：
 * 確保角色在電腦工作區、敏捷看板、測試區與圍爐裏茶歇區之間移動時，
 * 100% 沿著杉木走廊與榻榻米走道繞行，絕不穿模踩過炭火圍爐裏或穿透辦公桌！
 */
export function buildIsoWalkPath(fromX: number, fromY: number, toX: number, toY: number): { x: number; y: number }[] {
  const nav = findNavPath({ x: fromX, y: fromY }, { x: toX, y: toY });
  return [{ x: fromX, y: fromY }, ...nav];
}

function getPathLength(pts: { x: number; y: number }[]): number {
  let total = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    total += Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
  }
  return total;
}

function interpolateWalk(
  fromX: number,
  fromY: number,
  toX: number,
  toY: number,
  u: number
): { x: number; y: number; vx: number; vy: number; facing: 'left' | 'right' | 'front' } {
  const clamped = Math.max(0, Math.min(1, u));
  // 起步與停步平滑加減速（Ease-In-Out），中段保持穩定物理步速
  const smooth = clamped * clamped * (3 - 2 * clamped);
  const pts = buildIsoWalkPath(fromX, fromY, toX, toY);
  const totalLen = Math.max(0.001, getPathLength(pts));
  const targetDist = smooth * totalLen;

  let acc = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const segLen = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
    if (acc + segLen >= targetDist || i === pts.length - 2) {
      const segU = segLen > 0.0001 ? Math.max(0, Math.min(1, (targetDist - acc) / segLen)) : 1;
      const x = pts[i].x + (pts[i + 1].x - pts[i].x) * segU;
      const y = pts[i].y + (pts[i + 1].y - pts[i].y) * segU;
      const vx = pts[i + 1].x - pts[i].x;
      const vy = pts[i + 1].y - pts[i].y;
      const facing: 'left' | 'right' | 'front' = vx < -0.5 ? 'left' : vx > 0.5 ? 'right' : 'front';
      return { x, y, vx, vy, facing };
    }
    acc += segLen;
  }

  return { x: toX, y: toY, vx: 0, vy: 0, facing: 'front' };
}

function computeActorState(
  roomId: string,
  slotKey: PersonaSlotKey,
  standbySpot: string,
  targetSpot: string,
  targetPose: string,
  now: number
): { x: number; y: number; vx: number; vy: number; pose: string; facing: 'left' | 'right' | 'front'; walking: boolean } {
  const key = `${roomId}:${slotKey}`;
  const dest = SPOT[targetSpot] || SPOT.center;
  const standby = SPOT[standbySpot] || dest;
  const prev = actorTransitions.get(key);

  if (!prev) {
    const isWorking = targetSpot !== standbySpot;
    const pathLen = getPathLength(buildIsoWalkPath(standby.x, standby.y, dest.x, dest.y));
    const durationMs = isWorking && pathLen > 2 ? Math.min(2800, Math.max(1200, Math.round(pathLen * 28))) : 0;
    const init: ActorTransition = {
      fromX: isWorking ? standby.x : dest.x,
      fromY: isWorking ? standby.y : dest.y,
      toX: dest.x,
      toY: dest.y,
      targetSpot,
      targetPose,
      startMs: now,
      durationMs,
    };
    actorTransitions.set(key, init);
    if (durationMs > 0) {
      const step = interpolateWalk(init.fromX, init.fromY, init.toX, init.toY, 0.02);
      return { x: step.x, y: step.y, vx: step.vx, vy: step.vy, pose: 'walk', facing: step.facing, walking: true };
    }
    return { x: dest.x, y: dest.y, vx: 0, vy: 0, pose: targetPose, facing: 'front', walking: false };
  }

  if (prev.targetSpot !== targetSpot) {
    const elapsed = Math.max(0, now - prev.startMs);
    const cur =
      prev.durationMs > 0 && elapsed < prev.durationMs
        ? interpolateWalk(prev.fromX, prev.fromY, prev.toX, prev.toY, elapsed / prev.durationMs)
        : { x: prev.toX, y: prev.toY, vx: 0, vy: 0, facing: 'front' as const };
    const pathLen = getPathLength(buildIsoWalkPath(cur.x, cur.y, dest.x, dest.y));
    const durationMs = pathLen > 2 ? Math.min(2800, Math.max(1100, Math.round(pathLen * 28))) : 0;
    const next: ActorTransition = {
      fromX: cur.x,
      fromY: cur.y,
      toX: dest.x,
      toY: dest.y,
      targetSpot,
      targetPose,
      startMs: now,
      durationMs,
    };
    actorTransitions.set(key, next);
    if (durationMs > 0) {
      const step = interpolateWalk(next.fromX, next.fromY, next.toX, next.toY, 0.02);
      return { x: step.x, y: step.y, vx: step.vx, vy: step.vy, pose: 'walk', facing: step.facing, walking: true };
    }
    return { x: dest.x, y: dest.y, vx: 0, vy: 0, pose: targetPose, facing: 'front', walking: false };
  }

  prev.targetPose = targetPose;
  const elapsed = Math.max(0, now - prev.startMs);
  if (prev.durationMs > 0 && elapsed < prev.durationMs) {
    const step = interpolateWalk(prev.fromX, prev.fromY, prev.toX, prev.toY, elapsed / prev.durationMs);
    return { x: step.x, y: step.y, vx: step.vx, vy: step.vy, pose: 'walk', facing: step.facing, walking: true };
  }

  return { x: dest.x, y: dest.y, vx: 0, vy: 0, pose: targetPose, facing: 'front', walking: false };
}

function resolveSeatModel(
  room: TeamRoom,
  role: 'supervisor' | 'impl' | 'review',
  defaultLabel?: string
): { seat: string; seatName: string; model: string; fullLabel: string } {
  const pref =
    role === 'supervisor'
      ? room.supervisor_pref || room.role_prefs?.supervisor
      : role === 'impl'
        ? room.impl_pref || room.role_prefs?.impl
        : room.review_pref || room.role_prefs?.review;
  const cliVal =
    role === 'supervisor'
      ? room.supervisor_cli
      : role === 'impl'
        ? room.impl_cli
        : room.review_cli;
  const seatVal =
    role === 'supervisor'
      ? room.supervisor_seat
      : role === 'impl'
        ? room.impl_seat
        : room.review_seat;
  const rawSeat = (pref && pref.seat) || (cliVal && cliVal !== 'auto' ? cliVal : seatVal) || 'auto';
  const seat = SEAT_META[rawSeat] ? rawSeat : 'auto';
  const seatName = SEAT_META[seat]?.name || rawSeat;
  const actual =
    role === 'supervisor'
      ? room.supervisor_model_actual || ''
      : role === 'impl'
        ? room.impl_model_actual || ''
        : room.review_model_actual || '';
  const asked =
    (pref && pref.model) ||
    (role === 'supervisor' ? room.supervisor_model : role === 'impl' ? room.impl_model : room.review_model) ||
    '';
  const isRunning = room.status === 'running';
  const pendingNext = Boolean(isRunning && asked && actual && asked !== actual);
  const defaultModel =
    seat === 'glm' || seat === 'glm_high'
      ? 'glm-5.3'
      : seat === 'litellm'
        ? '線上模型'
        : '預設模型';
  const model = asked || actual || defaultModel;
  let fullLabel: string;
  if (seat === 'auto') {
    fullLabel = asked
      ? `自動 · ${asked}${pendingNext ? '（下回合生效）' : ''}`
      : role === 'supervisor'
        ? defaultLabel || 'Decision-1 / GLM-5.3'
        : 'Decision-1 自動派工';
  } else {
    fullLabel = `${seatName} · ${model}${pendingNext ? '（下回合生效）' : ''}`;
  }
  return { seat, seatName, model, fullLabel };
}

function lastEventMatch(room: TeamRoom, pred: (e: TeamEvent) => boolean): TeamEvent | null {
  const desc = sortEventsDesc(Array.isArray(room.events) ? room.events : []);
  for (const ev of desc) {
    if (pred(ev)) return ev;
  }
  return null;
}

export function buildRoomScene(
  room: TeamRoom,
  inboxItems: InboxItem[],
  now: number = Date.now(),
  advancePhysics = false
) {
  const st = room.status;
  const ph = room.phase || 'goal';
  const running = st === 'running';
  const off = st === 'queued' || st === 'paused';

  const implInfo = resolveSeatModel(room, 'impl');
  const revInfo = resolveSeatModel(room, 'review');

  // 依 room.id 與換角種子從 48 角色庫選出本房間專屬且完全不重複的 5 位角色陣容（3 人類工作角色 + 1 犬 + 1 貓）
  const cast = pickRoomCast(room.id);
  const theme = pickRoomTheme(room.id);
  const solar = computeSolarLight(now);

  const screens = {
    impl:
      implInfo.seat === 'auto' || off
        ? 'idle'
        : st === 'error'
          ? 'err'
          : st === 'waiting_user'
            ? 'wait'
            : st === 'done'
              ? 'ok'
              : ph === 'build'
                ? 'code'
                : ph === 'spec'
                  ? 'doc'
                  : 'idle',
    rev: revInfo.seat === 'auto' || off ? 'idle' : st === 'done' ? 'ok' : running && ph === 'review' ? 'diff' : 'idle',
    test: running && ph === 'test' ? 'run' : ph === 'review' || ph === 'done' ? 'green' : 'off',
  };

  const activeCriterionIndex = findActiveCriterionIndex(room.criteria, room);
  const crit = (room.criteria || []).map((c, idx) => {
    if (Boolean(c?.done) || c?.status === 'verified' || ph === 'review' || ph === 'done') return 'ok';
    if (ph === 'test' || (running && idx === activeCriterionIndex)) return 'run';
    return 'todo';
  });

  // Supervisor Persona (PO / Tech Lead / Scrum Master / QA)
  const rawPersona = room.persona as PersonaId | undefined;
  const supervisorId: PersonaId =
    rawPersona && rawPersona in PERSONA_CATALOG && PERSONA_CATALOG[rawPersona].tier === 'supervisor'
      ? rawPersona
      : ph === 'goal' || ph === 'spec'
        ? 'po'
        : ph === 'test'
          ? 'qa'
          : 'scrum';
  const supSpec = PERSONA_CATALOG[supervisorId] || PERSONA_CATALOG.po;
  const supWorkSpot =
    supervisorId === 'po' || supervisorId === 'tech_lead'
      ? 'board'
      : supervisorId === 'qa' || ph === 'test'
        ? 'test'
        : 'board';
  const supHasWork =
    (running && (ph === 'goal' || ph === 'spec' || ph === 'test' || rawPersona === supervisorId)) ||
    st === 'waiting_user' ||
    inboxItems.length > 0;
  const supTargetSpot = supHasWork ? supWorkSpot : 'coffeeA';
  const supTargetPose = supHasWork ? (supWorkSpot === 'board' && running ? 'write' : 'stand') : 'mug';
  const lastRoleSay = lastEventMatch(room, (e) => e.kind === 'say' && (!e.role || e.role === supervisorId));
  const supSay =
    lastRoleSay?.text?.replace(/^【[^】]*】/, '').trim() ||
    (st === 'waiting_user' && inboxItems[0]?.title
      ? inboxItems[0].title
      : supHasWork
        ? supSpec.defaultSay
        : theme.season === 'summer'
          ? '在和室茶几旁喝冰麥茶待命'
          : '在圍爐裏茶歇待命');

  // Developer Implementer / Fixer Persona
  const devPersonaId: PersonaId = rawPersona === 'fixer' ? 'fixer' : 'implementer';
  const devSpec = PERSONA_CATALOG[devPersonaId];
  const isFallback = room.impl_by === 'fallback';
  const implHasWork =
    st === 'error' ||
    (running && (ph === 'build' || ph === 'spec' || rawPersona === 'implementer' || rawPersona === 'fixer'));
  const implMark = st === 'error' ? 'err' : st === 'waiting_user' || inboxItems.length > 0 ? 'wait' : '';
  const implTargetSpot = implHasWork ? 'implSeat' : 'sofaA';
  const implTargetPose =
    st === 'error'
      ? 'head'
      : implHasWork
        ? 'type'
        : st === 'done'
          ? 'cheer'
          : 'sofa';
  const lastToolEv = lastEventMatch(room, (e) => e.kind === 'tool' || e.kind === 'agent');
  const implBubble =
    st === 'waiting_user' || inboxItems.length > 0
      ? inboxItems[0]?.title || room.activity || '在和室待命區等你決定下一步'
      : st === 'error'
        ? room.error || '執行發生錯誤，請檢查'
        : st === 'paused'
          ? theme.season === 'summer'
            ? '已暫停，在和室茶几旁吃西瓜納涼'
            : '已暫停，在榻榻米地爐旁休息'
          : st === 'queued'
            ? '在榻榻米區排隊等待可用座位'
            : st === 'done'
              ? '任務已完成，在和室慶祝休息！'
              : implHasWork
                ? lastToolEv?.text || room.activity || devSpec.defaultSay
                : '在榻榻米休息待命';

  // Reviewer Persona
  const revSpec = PERSONA_CATALOG.reviewer;
  const revHasWork = running && (ph === 'review' || rawPersona === 'reviewer');
  const revTargetSpot = revHasWork ? 'revSeat' : 'sofaB';
  const revTargetPose = revHasWork ? 'type' : st === 'done' ? 'cheer' : 'sofa';
  const revBubble =
    st === 'done'
      ? '審查通過，在和室喝茶休息'
      : off
        ? '在榻榻米休息（隨房間暫停）'
        : revHasWork
          ? room.activity || '正在唯讀審查程式碼 diff'
          : `在待命區待命（${revInfo.seatName}）`;

  const implEngineLabel = isFallback
    ? `GLM-5.3（代班 ${room.fallback_from || 'CLI'}）`
    : implInfo.fullLabel;

  const dogChar = cast.companions[0];
  const catChar = cast.companions[1];

  // === Matter.js + Pathfinding A* + Utility AI 物理引擎步進（解耦 React render 副作用！）===
  const physicsIntent: RoomPhysicsIntent = {
    roomId: room.id,
    season: theme.season,
    isNight: solar.isNight,
    isRainOrSnow:
      theme.weather === 'rain' ||
      theme.weather === 'thunder' ||
      theme.weather === 'snow' ||
      theme.weather === 'blizzard',
    isRoomIdle: !running && st !== 'error',
    humans: {
      supervisor: {
        standbySpot: 'coffeeA',
        targetSpot: supTargetSpot,
        targetPose: supTargetPose,
        isWorking: supHasWork,
        hasError: false,
      },
      implementer: {
        standbySpot: 'sofaA',
        targetSpot: implTargetSpot,
        targetPose: implTargetPose,
        isWorking: implHasWork,
        hasError: st === 'error',
      },
      reviewer: {
        standbySpot: 'sofaB',
        targetSpot: revTargetSpot,
        targetPose: revTargetPose,
        isWorking: revHasWork,
        hasError: false,
      },
    },
    pets: {
      dog: {
        charId: dogChar.id,
        nativePosture: getPetNativePosture(dogChar.id),
        trait: dogChar.breedTrait || 'herding_dash',
      },
      cat: {
        charId: catChar.id,
        nativePosture: getPetNativePosture(catChar.id),
        trait: catChar.breedTrait || 'cat_loaf',
      },
    },
    spots: getSeasonalSpots(theme.season),
  };

  const phys = advancePhysics ? stepRoomPhysics(physicsIntent, now) : peekRoomPhysics(physicsIntent, now);
  const supMotion = phys.humans.supervisor;
  const implMotion = phys.humans.implementer;
  const revMotion = phys.humans.reviewer;
  const dogPhys = phys.pets.dog;
  const catPhys = phys.pets.cat;

  const supEngineInfo = resolveSeatModel(room, 'supervisor', supSpec.defaultEngine);
  const supEngineLabel =
    (room.engine_version || 'v2') === 'v2'
      ? 'LangGraph ＋ Decision-1 (128 題閘門)'
      : supEngineInfo.fullLabel.includes('Decision-1')
        ? supEngineInfo.fullLabel
        : `Decision-1 ＋ ${supEngineInfo.fullLabel}`;

  const personas: RoomPersona[] = [
    {
      id: supervisorId,
      slotKey: 'supervisor',
      tier: 'supervisor',
      tierLabel: '高層主管',
      roleTitle: `高層主管（${cast.supervisor.name}）`,
      engineLabel: supEngineLabel,
      badgeText: 'SUP',
      badgeColor: supSpec.shirt,
      gender: supSpec.gender,
      x: supMotion.x,
      y: supMotion.y,
      vx: supMotion.vx,
      vy: supMotion.vy,
      pose: supMotion.pose,
      posturePhase: supMotion.posturePhase,
      seatBlend: supMotion.seatBlend,
      useBack: supMotion.useBack,
      facing: supMotion.facing,
      shirt: cast.supervisor.shirt,
      look: { hair: cast.supervisor.hair, style: cast.supervisor.style, skin: cast.supervisor.skin },
      character: cast.supervisor,
      bubble: supSay,
      bubbleTone: st === 'waiting_user' || inboxItems.length > 0 ? 'wait' : '',
      isWorking: supHasWork,
      recentRecords: getPersonaRecentRecords(room, undefined, 'supervisor', 5),
    },
    {
      id: devPersonaId,
      slotKey: 'implementer',
      tier: 'developer',
      tierLabel: isFallback ? 'GLM 代班研發' : '研發工程師',
      roleTitle: `研發工程師（${cast.implementer.name}）`,
      engineLabel: implEngineLabel,
      badgeText: isFallback ? 'G⚡' : SEAT_META[implInfo.seat]?.g || 'D',
      badgeColor: isFallback ? '#62d07f' : SEAT_META[implInfo.seat]?.c || devSpec.shirt,
      gender: devSpec.gender,
      x: implMotion.x,
      y: implMotion.y,
      vx: implMotion.vx,
      vy: implMotion.vy,
      pose: implMotion.pose,
      posturePhase: implMotion.posturePhase,
      seatBlend: implMotion.seatBlend,
      useBack: implMotion.useBack,
      facing: implMotion.facing,
      shirt: cast.implementer.shirt,
      look: { hair: cast.implementer.hair, style: cast.implementer.style, skin: cast.implementer.skin },
      character: cast.implementer,
      mark: implMark,
      bubble: implBubble,
      bubbleTone: st === 'error' ? 'err' : st === 'waiting_user' || inboxItems.length > 0 ? 'wait' : st === 'done' ? 'ok' : '',
      isFallback,
      isWorking: implHasWork,
      recentRecords: getPersonaRecentRecords(room, undefined, 'implementer', 5),
    },
    {
      id: 'reviewer',
      slotKey: 'reviewer',
      tier: 'developer',
      tierLabel: '審查工程師',
      roleTitle: `審查工程師（${cast.reviewer.name}）`,
      engineLabel: revInfo.fullLabel,
      badgeText: SEAT_META[revInfo.seat]?.g || 'R',
      badgeColor: SEAT_META[revInfo.seat]?.c || revSpec.shirt,
      gender: revSpec.gender,
      x: revMotion.x,
      y: revMotion.y,
      vx: revMotion.vx,
      vy: revMotion.vy,
      pose: revMotion.pose,
      posturePhase: revMotion.posturePhase,
      seatBlend: revMotion.seatBlend,
      useBack: revMotion.useBack,
      facing: revMotion.facing,
      shirt: cast.reviewer.shirt,
      look: { hair: cast.reviewer.hair, style: cast.reviewer.style, skin: cast.reviewer.skin },
      character: cast.reviewer,
      bubble: revBubble,
      bubbleTone: st === 'done' ? 'ok' : '',
      isWorking: revHasWork,
      recentRecords: getPersonaRecentRecords(room, undefined, 'reviewer', 5),
    },
  ];

  const companions: CompanionSpec[] = [
    {
      id: 'shiba',
      name: `${dogChar.name}（${dogChar.bio}）`,
      species: 'dog',
      x: dogPhys.x,
      y: dogPhys.y,
      pose: dogPhys.behavior === 'walking' || dogPhys.behavior === 'trotting' || dogPhys.behavior === 'chase_ball' ? 'trot' : dogPhys.behavior,
      vx: dogPhys.vx,
      vy: dogPhys.vy,
      facingLeft: dogPhys.facingLeft,
      behavior: dogPhys.behavior,
      gait: dogPhys.gait,
      interactionHeart: dogPhys.interactionHeart,
      defaultAction: dogPhys.actionLabel,
      character: dogChar,
    },
    {
      id: 'calico',
      name: `${catChar.name}（${catChar.bio}）`,
      species: 'cat',
      x: catPhys.x,
      y: catPhys.y,
      pose: catPhys.behavior === 'walking' || catPhys.behavior === 'trotting' ? 'trot' : catPhys.behavior,
      vx: catPhys.vx,
      vy: catPhys.vy,
      facingLeft: catPhys.facingLeft,
      behavior: catPhys.behavior,
      gait: catPhys.gait,
      interactionHeart: catPhys.interactionHeart,
      defaultAction: catPhys.actionLabel,
      character: catChar,
    },
  ];

  return {
    lit: true,
    bang: st === 'waiting_user' || st === 'error' || inboxItems.length > 0,
    theme,
    solar,
    screens,
    toyBall: phys.toyBall,
    board: {
      crit,
      commits: st === 'done' ? 3 : ph === 'review' ? 2 : ph === 'build' ? 1 : 0,
      writing: supMotion.pose === 'write',
    },
    personas,
    companions,
    implInfo,
    revInfo,
    supervisorSpec: supSpec,
    activeCriterionIndex,
  };
}

/**
 * 移除先前以 fpx() 方塊疊加在地板與桌面上的粗糙色塊，
 * 100% 保留 iso-room-clean.png 原生高精細像素畫質（無任何不明方塊）。
 */
function drawOfficeLifeAndPetProps(
  _ctx: CanvasRenderingContext2D,
  _room: TeamRoom,
  _scene: ReturnType<typeof buildRoomScene>,
  _now: number
) {
  // 不繪製粗糙 fpx 色塊，維持古民家木地板、榻榻米與雙螢幕桌面的純淨高解析細節
}

export type SeasonKey = 'spring' | 'summer' | 'autumn' | 'winter';

export interface ForegroundFurnitureLayer {
  id: string;
  y: number;
  poly: Array<[number, number]>;
}

/** 依季節回傳等距房間前景遮擋圖層（雙螢幕桌、冬暖桌、夏茶几／立扇、春秋地爐邊框）。 */
export function getForegroundFurnitureLayers(season: SeasonKey): ForegroundFurnitureLayer[] {
  const commonDesks: ForegroundFurnitureLayer[] = [
    {
      id: 'left_dual_monitor_desk',
      y: 77.0,
      poly: [
        [71, 56],
        [95, 56],
        [95, 79],
        [71, 79],
      ],
    },
    {
      id: 'right_dual_monitor_desk',
      y: 65.5,
      poly: [
        [98, 44],
        [121, 44],
        [121, 66],
        [98, 66],
      ],
    },
  ];
  if (season === 'winter') {
    return [
      ...commonDesks,
      {
        id: 'winter_kotatsu',
        y: 89.5,
        poly: [
          [135, 70.0],
          [155, 80.2],
          [161, 91.1],
          [135, 103.8],
          [109, 91.1],
          [116, 80.2],
        ],
      },
    ];
  }
  if (season === 'summer') {
    return [
      ...commonDesks,
      {
        id: 'summer_table',
        y: 88.5,
        poly: [
          [138, 76.0],
          [155, 84.4],
          [154, 92.0],
          [138, 100.4],
          [122, 92.0],
          [121, 84.4],
        ],
      },
      {
        id: 'summer_standing_fan',
        y: 82.0,
        poly: [
          [172, 64.0],
          [186, 64.0],
          [186, 86.0],
          [172, 86.0],
        ],
      },
    ];
  }
  return [
    ...commonDesks,
    {
      id: 'irori_hearth_rim',
      y: 90.0,
      poly: [
        [137, 81.5],
        [147, 86.8],
        [147, 94.5],
        [137, 97.5],
        [128, 94.5],
        [128, 86.8],
      ],
    },
  ];
}

/** 就座人類細微工作／呼吸微動畫偏移（鎖定座墊錨點，有界不漂移）。 */
export function computeSeatedMicroOffset(isWorking: boolean, phaseMs: number, seed = 0): { dx: number; dy: number } {
  const t = phaseMs / 1000 + seed * 0.37;
  if (isWorking) {
    return {
      dx: Math.sin(t * 7.5) * 0.25,
      dy: Math.sin(t * 3.8) * 0.38,
    };
  }
  return {
    dx: 0,
    dy: Math.sin(t * 1.6) * 0.22,
  };
}

/** 離線依電腦真實月份與南北半球推算真實季節（零網路延遲）。 */
export function resolveSeasonForDate(date: Date = new Date(), hemisphere: 'north' | 'south' = 'north'): SeasonKey {
  const m = date.getMonth() + 1; // 1..12
  let north: SeasonKey = 'winter';
  if (m >= 3 && m <= 5) north = 'spring';
  else if (m >= 6 && m <= 8) north = 'summer';
  else if (m >= 9 && m <= 11) north = 'autumn';
  if (hemisphere === 'north') return north;
  const flip: Record<SeasonKey, SeasonKey> = {
    spring: 'autumn',
    summer: 'winter',
    autumn: 'spring',
    winter: 'summer',
  };
  return flip[north];
}

export function drawRoomCanvas(
  ctx: CanvasRenderingContext2D,
  room: TeamRoom,
  scene: ReturnType<typeof buildRoomScene>,
  now: number,
  selected: boolean,
  selectedPersonaSlot?: PersonaSlotKey | null
) {
  ensureAssetsLoaded();
  const theme = scene.theme || pickRoomTheme(room.id);
  const solar = scene.solar || computeSolarLight(now);

  // 1. 依四季切換 4 套專屬高精細等距像素基底房間（春櫻和室／夏西瓜電扇木茶几／秋楓炭火地爐／冬格紋暖桌被爐）
  const baseKey: AssetKey =
    theme.season === 'spring' && assetReady.roomSpring
      ? 'roomSpring'
      : theme.season === 'summer' && assetReady.roomSummer
        ? 'roomSummer'
        : theme.season === 'autumn' && assetReady.roomAutumn
          ? 'roomAutumn'
          : theme.season === 'winter' && assetReady.roomWinter
            ? 'roomWinter'
            : 'clean';
  const baseImg = assetCache[baseKey];
  if (assetReady[baseKey] && baseImg && typeof ctx.drawImage === 'function') {
    ctx.drawImage(baseImg, 0, 0, LW, LH);
  } else {
    paintBackground(ctx, room.id, solar.isNight);
  }

  // 2. 專屬四季房間已具精細座墊與季節擺設，僅於降級回原圖時補色；其餘疊加柔和季節環境光氛
  if (baseKey === 'clean') {
    drawSeasonalIndoorCushions(ctx, theme.cushionMain, theme.indoorAmbient);
  } else if (theme.indoorAmbient) {
    ctx.save();
    ctx.fillStyle = theme.indoorAmbient;
    ctx.fillRect(0, 0, LW, LH);
    ctx.restore();
  }

  // 3. 選取對應的高精細 16-bit 像素窗景圖（都會高樓 101 日夜／湘南海港／高山雪峰雲海／春櫻庭園），疊加陰雨天雲幕與 SunCalc 日照光束
  const vistaKind = pickWindowVista(theme, room.id);
  let vistaImg: HTMLImageElement | null = null;
  if (vistaKind === 'skyline') {
    vistaImg =
      solar.isNight && assetReady.vistaSkylineNight
        ? assetCache.vistaSkylineNight || null
        : assetReady.vistaSkyline
          ? assetCache.vistaSkyline || null
          : null;
  } else if (vistaKind === 'harbor' && assetReady.vistaHarbor) {
    vistaImg = assetCache.vistaHarbor || null;
  } else if (vistaKind === 'alpine' && assetReady.vistaAlpine) {
    vistaImg = assetCache.vistaAlpine || null;
  } else if (vistaKind === 'garden' && theme.season === 'spring' && assetReady.vistaSakura) {
    vistaImg = assetCache.vistaSakura || null;
  }
  drawRichWindowAndSunShafts(ctx, theme, room.id, solar, now, vistaImg);

  // 4. 春秋顯示地爐 Simplex-Noise 動態炭火與火星；夏天（西瓜木茶几）與冬天（暖桌被爐）無明火
  if (theme.season === 'spring' || theme.season === 'autumn') {
    drawDynamicHearthFire(ctx, theme.season, solar, now);
  }

  // 5. 疊加即時動態微物理圖層（覆蓋靜態指針的精準時鐘含紅秒針、手沖咖啡與手沖壺捲曲熱氣、雙螢幕動態程式碼）
  drawAccurateClock(ctx, now);
  drawCoffeeSteam(ctx, now);
  drawBoard(ctx, scene.board, now);
  drawScreens(ctx, scene.screens, now);

  // 6. 物理滾動玩具網球（由 Matter.js 剛體與狗狗碰撞推進）
  if (scene.toyBall && typeof ctx.beginPath === 'function') {
    ctx.save();
    ctx.fillStyle = 'rgba(24, 15, 9, 0.35)';
    ctx.beginPath();
    ctx.ellipse(scene.toyBall.x, scene.toyBall.y + 0.8, 1.8, 0.9, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#a3e635';
    ctx.strokeStyle = '#365314';
    ctx.lineWidth = 0.4;
    ctx.beginPath();
    ctx.arc(scene.toyBall.x, scene.toyBall.y - 0.5, 1.35, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // 7. 依等距 Y 深度由遠到近（Painter's Algorithm）繪製房間內的 3 位人類工作角色 ＋ 2 位純天然犬貓吉祥物 ＋ 季節家具前景圖層
  type DrawableEntity =
    | { kind: 'persona'; y: number; p: RoomPersona }
    | { kind: 'companion'; y: number; c: CompanionSpec }
    | { kind: 'furniture'; y: number; poly: Array<[number, number]> };

  const entities: DrawableEntity[] = [
    ...scene.personas.map((p) => ({ kind: 'persona' as const, y: p.y, p })),
    ...scene.companions.map((c) => ({ kind: 'companion' as const, y: c.y, c })),
  ];
  if (baseImg) {
    const activeSeasonKey: SeasonKey =
      baseKey === 'roomWinter'
        ? 'winter'
        : baseKey === 'roomSummer'
          ? 'summer'
          : baseKey === 'roomAutumn'
            ? 'autumn'
            : 'spring';
    for (const fl of getForegroundFurnitureLayers(activeSeasonKey)) {
      entities.push({ kind: 'furniture', y: fl.y, poly: fl.poly });
    }
  }
  entities.sort((a, b) => a.y - b.y);

  for (const ent of entities) {
    if (ent.kind === 'furniture') {
      if (baseImg && typeof ctx.save === 'function' && typeof ctx.clip === 'function' && typeof ctx.drawImage === 'function') {
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(ent.poly[0][0], ent.poly[0][1]);
        for (let i = 1; i < ent.poly.length; i++) ctx.lineTo(ent.poly[i][0], ent.poly[i][1]);
        ctx.closePath();
        ctx.clip();
        ctx.drawImage(baseImg, 0, 0, LW, LH);
        ctx.restore();
      }
      continue;
    }
    if (ent.kind === 'companion') {
      const compChar = ent.c.character || CHARACTER_ROSTER[24];
      drawCharacterSprite(
        ctx,
        compChar,
        ent.c.x,
        ent.c.y,
        ent.c.pose || 'stand',
        now + hash(ent.c.id) * 13,
        false,
        1.0,
        ent.c.vx || 0,
        ent.c.vy || 0,
        Boolean(ent.c.interactionHeart),
        {
          solar,
          gait: ent.c.gait,
          facingLeft: ent.c.facingLeft,
          behavior: ent.c.behavior,
          useBackHint: (ent.c as { facingBack?: boolean }).facingBack,
        }
      );
    } else {
      const p = ent.p;
      const isSeatedDraw = p.pose === 'type' || p.pose === 'sit' || p.pose === 'head' || p.pose === 'sofa' || (p.seatBlend ?? 0) > 0.6;
      const micro = isSeatedDraw ? computeSeatedMicroOffset(p.pose === 'type' || p.pose === 'write', now, hash(p.slotKey) % 19) : { dx: 0, dy: 0 };
      drawCharacterSprite(
        ctx,
        p.character,
        p.x + micro.dx,
        p.y + micro.dy,
        p.pose,
        now + (hash(p.slotKey) % 500),
        selectedPersonaSlot === p.slotKey,
        1.0,
        p.vx || 0,
        p.vy || 0,
        false,
        {
          solar,
          seatBlend: p.seatBlend,
          useBackHint: p.useBack,
        }
      );
      // 當人類角色坐進雙螢幕桌前（implSeat / revSeat）或正在平滑入座（seatBlend > 0.25）時，同步繪製椅背前景遮罩
      const seatActive = p.pose === 'type' || p.pose === 'sit' || p.pose === 'head' || (p.seatBlend ?? 0) > 0.25;
      if (seatActive) {
        if (Math.hypot(p.x - SPOT.implSeat.x, p.y - SPOT.implSeat.y) < 5) {
          drawChairBackrestOverlay(ctx, 'impl');
        } else if (Math.hypot(p.x - SPOT.revSeat.x, p.y - SPOT.revSeat.y) < 5) {
          drawChairBackrestOverlay(ctx, 'rev');
        }
      }
    }
  }

  for (const p of scene.personas) {
    if (p.mark) mark(ctx, p.x + 6, p.y - 21, p.mark, now);
  }

  if (!scene.lit) {
    ctx.fillStyle = 'rgba(18, 12, 8, 0.42)';
    ctx.fillRect(0, 0, LW, LH);
  }
  if (scene.bang) roomBang(ctx, now);
}

export function drawEmptyRoomCanvas(ctx: CanvasRenderingContext2D, slotKey: string, now: number) {
  ensureAssetsLoaded();
  const solar = computeSolarLight(now);
  const emptyImg = assetCache.clean;
  if (assetReady.clean && emptyImg && typeof ctx.drawImage === 'function') {
    ctx.drawImage(emptyImg, 0, 0, LW, LH);
  } else {
    paintBackground(ctx, `empty:${slotKey}`, solar.isNight);
  }
  drawAccurateClock(ctx, now);
  drawCoffeeSteam(ctx, now);
  ctx.fillStyle = 'rgba(16, 11, 7, 0.38)';
  ctx.fillRect(0, 0, LW, LH);
}
