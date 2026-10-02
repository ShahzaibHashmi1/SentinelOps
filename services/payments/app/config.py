from functools import lru_cache

from pydantic import model_validator

from common.settings import Settings


class PaymentsSettings(Settings):
    """Common settings plus the simulated processing delay (milliseconds, random in [min, max])."""

    payment_delay_ms_min: int = 20
    payment_delay_ms_max: int = 80

    @model_validator(mode="after")
    def _check_delay(self) -> "PaymentsSettings":
        if not 0 <= self.payment_delay_ms_min <= self.payment_delay_ms_max:
            raise ValueError("need 0 <= PAYMENT_DELAY_MS_MIN <= PAYMENT_DELAY_MS_MAX")
        return self


@lru_cache
def get_payments_settings() -> PaymentsSettings:
    return PaymentsSettings()
