import { WebhookErrorFragment } from "@saleor/fragments/types/WebhookErrorFragment";
import { IntlShape } from "react-intl";

import { getCommonFormFieldErrorMessage } from "./common";

function getWebhookErrorMessage(
  err: Omit<WebhookErrorFragment, "__typename"> | undefined,
  intl: IntlShape
): string {
  return getCommonFormFieldErrorMessage(err, intl);
}

export default getWebhookErrorMessage;
