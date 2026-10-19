'use strict';

const path = require('path');
const protobuf = require('protobufjs');

const CRAFT_MESSAGE_ID = 1002;
const CraftItemsRequest = protobuf
  .loadSync(path.join(__dirname, 'craft_request.proto'))
  .lookupType('CMsgCraftItemsRequest');

function encodeCraftRequest(assetIds, recipe) {
  if (!Number.isInteger(recipe) || recipe < 0 || recipe > 0xffffffff) {
    throw new Error('汰换配方编号无效');
  }
  if (!Array.isArray(assetIds) || ![5, 10].includes(assetIds.length)) {
    throw new Error('汰换材料数量无效');
  }
  const ids = assetIds.map((id) => {
    // Never convert a 64-bit asset ID to a JavaScript Number.
    if (typeof id !== 'string' || !/^[1-9][0-9]*$/.test(id)
        || BigInt(id) > 0xffffffffffffffffn) {
      throw new Error('汰换材料资产编号无效');
    }
    return id;
  });
  if (new Set(ids).size !== ids.length) {
    throw new Error('汰换材料资产编号重复');
  }
  const message = CraftItemsRequest.fromObject({recipeDef: recipe, craftItems: ids});
  return CraftItemsRequest.encode(message).finish();
}

module.exports = {CRAFT_MESSAGE_ID, encodeCraftRequest};
