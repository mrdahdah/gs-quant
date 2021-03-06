"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import datetime as dt
import logging
import weakref
from abc import ABCMeta
from concurrent.futures import ThreadPoolExecutor
from inspect import signature
from threading import Lock
from typing import Iterable, Optional, Union

from .markets import CloseMarket, LiveMarket, Market
from gs_quant.base import Priceable, RiskKey, Scenario, get_enum_value
from gs_quant.common import PricingLocation
from gs_quant.context_base import ContextBaseWithDefault
from gs_quant.datetime.date import business_day_offset
from gs_quant.risk import CompositeScenario, DataFrameWithInfo, ErrorValue, FloatWithInfo, MarketDataScenario, \
    RiskMeasure, StringWithInfo
from gs_quant.risk.results import PricingFuture
from gs_quant.session import GsSession
from gs_quant.target.common import PricingDateAndMarketDataAsOf
from gs_quant.target.risk import RiskPosition, RiskRequest, RiskRequestParameters


_logger = logging.getLogger(__name__)

CacheResult = Union[DataFrameWithInfo, FloatWithInfo, StringWithInfo]


class PricingCache(metaclass=ABCMeta):
    """
    Weakref cache for instrument calcs
    """
    __cache = weakref.WeakKeyDictionary()

    @classmethod
    def clear(cls):
        __cache = weakref.WeakKeyDictionary()

    @classmethod
    def get(cls, risk_key: RiskKey, priceable: Priceable) -> Optional[CacheResult]:
        return cls.__cache.get(priceable, {}).get(risk_key)

    @classmethod
    def put(cls, risk_key: RiskKey, priceable: Priceable, result: CacheResult):
        if not isinstance(result, ErrorValue) and not isinstance(risk_key.market, LiveMarket):
            cls.__cache.setdefault(priceable, {})[risk_key] = result

    @classmethod
    def drop(cls, priceable: Priceable):
        if priceable in cls.__cache:
            cls.__cache.pop(priceable)


class PricingContext(ContextBaseWithDefault):

    """
    A context for controlling pricing and market data behaviour
    """

    def __init__(self,
                 pricing_date: Optional[dt.date] = None,
                 market_data_location: Optional[Union[PricingLocation, str]] = None,
                 is_async: bool = False,
                 is_batch: bool = False,
                 use_cache: bool = False,
                 visible_to_gs: bool = False,
                 csa_term: Optional[str] = None,
                 batch_results_timeout: Optional[int] = None,
                 market: Optional[Market] = None
                 ):
        """
        The methods on this class should not be called directly. Instead, use the methods on the instruments,
        as per the examples

        :param pricing_date: the date for pricing calculations. Default is today
        :param market_data_location: the location for sourcing market data ('NYC', 'LDN' or 'HKG' (defaults to LDN)
        :param is_async: if True, return (a future) immediately. If False, block (defaults to False)
        :param is_batch: use for calculations expected to run longer than 3 mins, to avoid timeouts.
            It can be used with is_aync=True|False (defaults to False)
        :param use_cache: store results in the pricing cache (defaults to False)
        :param visible_to_gs: are the contents of risk requests visible to GS (defaults to False)
        :param csa_term: the csa under which the calculations are made. Default is local ccy ois index

        **Examples**

        To change the market data location of the default context:

        >>> from gs_quant.markets import PricingContext
        >>> import datetime as dt
        >>>
        >>> PricingContext.current = PricingContext(market_data_location='LDN')

        For a blocking, synchronous request:

        >>> from gs_quant.instrument import IRCap
        >>> cap = IRCap('5y', 'GBP')
        >>>
        >>> with PricingContext():
        >>>     price_f = cap.dollar_price()
        >>>
        >>> price = price_f.result()

        For an asynchronous request:

        >>> with PricingContext(is_async=True):
        >>>     price_f = cap.dollar_price()
        >>>
        >>> while not price_f.done:
        >>>     ...
        """
        super().__init__()

        self.__pricing_date = pricing_date or business_day_offset(dt.date.today(), 0, roll='preceding')
        self.__csa_term = csa_term
        self.__is_async = is_async
        self.__is_batch = is_batch
        self.__batch_results_timeout = batch_results_timeout
        self.__use_cache = use_cache
        self.__visible_to_gs = visible_to_gs
        self.__market_data_location = get_enum_value(PricingLocation, market_data_location)
        self.__market = market or CloseMarket()
        self.__lock = Lock()
        self.__pending = {}

    def _on_exit(self, exc_type, exc_val, exc_tb):
        if exc_val:
            raise exc_val
        else:
            self.__calc()

    def __calc(self):
        session = GsSession.current
        requests_by_provider = {}

        def run_requests(requests_: Iterable[RiskRequest], provider_):
            results = {}

            try:
                with session:
                    results = provider_.calc_multi(requests_)
                    if self.__is_batch:
                        results = provider_.get_results(dict(zip(results, requests_)),
                                                        timeout=self.__batch_results_timeout).values()
            except Exception as e:
                results = ({k: e for k in self.__pending.keys()},)
            finally:
                with self.__lock:
                    for result in results:
                        for (risk_key, result_priceable), value in result.items():
                            if self.__use_cache:
                                PricingCache.put(risk_key, result_priceable, value)

                            self.__pending.pop((risk_key, result_priceable)).set_result(value)

        with self.__lock:
            # Group requests optimally
            for (key, priceable) in self.__pending.keys():
                requests_by_provider.setdefault(key.provider, {})\
                    .setdefault((key.params, key.scenario, key.date, key.market), {})\
                    .setdefault(priceable, set())\
                    .add(key.risk_measure)

        if requests_by_provider:
            num_providers = len(requests_by_provider)
            request_pool = ThreadPoolExecutor(num_providers) if num_providers > 1 or self.__is_async else None

            for provider, by_params_scenario_date_market in requests_by_provider.items():
                requests_for_provider = {}

                for (params, scenario, date, market), positions_by_measures in by_params_scenario_date_market.items():
                    for priceable, risk_measures in positions_by_measures.items():
                        requests_for_provider.setdefault((params, scenario, date, market, tuple(sorted(risk_measures))),
                                                         []).append(priceable)

                # TODO This will optimise for the fewest requests but we might want to just send one request per date
                requests_by_date_market = {}
                for (params, scenario, date, market, risk_measures), priceables in requests_for_provider.items():
                    requests_by_date_market.setdefault((params, scenario, risk_measures, tuple(priceables)), set())\
                        .add((date, market))

                requests = [
                    RiskRequest(
                        tuple(RiskPosition(instrument=p, quantity=p.get_quantity()) for p in priceables),
                        risk_measures,
                        parameters=self.__parameters,
                        wait_for_results=not self.__is_batch,
                        scenario=scenario,
                        pricing_and_market_data_as_of=tuple(PricingDateAndMarketDataAsOf(pricing_date=d, market=m)
                                                            for d, m in sorted(dates_markets)),
                        request_visible_to_gs=self.__visible_to_gs
                    )
                    for (params, scenario, risk_measures, priceables), dates_markets in requests_by_date_market.items()
                ]

                if request_pool:
                    request_pool.submit(run_requests, requests, provider)
                else:
                    run_requests(requests, provider)

            if request_pool:
                request_pool.shutdown(wait=not self.__is_async)

    def __risk_key(self, risk_measure: RiskMeasure, provider: type) -> RiskKey:
        return RiskKey(provider, self.__pricing_date, self.__market, self.__parameters, self.__scenario, risk_measure)

    @property
    def __parameters(self) -> RiskRequestParameters:
        return RiskRequestParameters(csa_term=self.__csa_term, raw_results=True)

    @property
    def __scenario(self) -> Optional[MarketDataScenario]:
        scenarios = Scenario.path
        if not scenarios:
            return None

        return MarketDataScenario(scenario=scenarios[0] if len(scenarios) == 1 else
                                  CompositeScenario(scenarios=tuple(reversed(scenarios))))

    @property
    def active_context(self):
        return next((c for c in reversed(PricingContext.path) if c.is_entered), self)

    @property
    def is_current(self) -> bool:
        return self == PricingContext.current

    @property
    def is_async(self) -> bool:
        return self.__is_async

    @property
    def is_batch(self) -> bool:
        return self.__is_batch

    @property
    def batch_results_timeout(self) -> Optional[int]:
        return self.__batch_results_timeout

    @property
    def market(self) -> Market:
        return self.__market

    @property
    def market_data_location(self) -> PricingLocation:
        return self.__market_data_location

    @property
    def csa_term(self) -> str:
        return self.__parameters.csa_term

    @property
    def pricing_date(self) -> dt.date:
        """Pricing date"""
        return self.__pricing_date

    @property
    def use_cache(self) -> bool:
        """Cache results"""
        return self.__use_cache

    @property
    def visible_to_gs(self) -> bool:
        """Request contents visible to GS"""
        return self.__visible_to_gs

    def clone(self, **kwargs):
        clone_kwargs = {k: getattr(self, k, None) for k in signature(self.__init__).parameters.keys()}
        clone_kwargs.update(kwargs)
        return self.__class__(**clone_kwargs)

    def calc(self, priceable: Priceable, risk_measure: RiskMeasure) -> PricingFuture:
        """
        Calculate the risk measure for the priceable instrument. Do not use directly, use via instruments

        :param priceable: The priceable (e.g. instrument)
        :param risk_measure: The measure we wish to calculate
        :return: A PricingFuture whose result will be the calculation result

        **Examples**

        >>> from gs_quant.instrument import IRSwap
        >>> from gs_quant.risk import IRDelta
        >>>
        >>> swap = IRSwap('Pay', '10y', 'USD', fixed_rate=0.01)
        >>> delta = swap.calc(IRDelta)
        """
        with self.active_context.__lock:
            risk_key = self.__risk_key(risk_measure, priceable.provider())
            future = self.active_context.__pending.get((risk_key, priceable))

            if future is None:
                future = PricingFuture()
                cached_result = PricingCache.get(risk_key, priceable) if self.use_cache else None

                if cached_result is not None:
                    future.set_result(cached_result)
                else:
                    self.active_context.__pending[(risk_key, priceable)] = future

        if not (self.is_entered or self.is_async):
            self.__calc()

        return future
