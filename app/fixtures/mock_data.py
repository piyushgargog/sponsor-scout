"""FICTIONAL demo companies for offline/mock mode. None of these are real organisations; they exist so the
pipeline (search -> research -> scoring -> contacts -> email) can be exercised without credentials.
The production code paths never read this module except through MockSearchProvider / FixtureFetcher."""
import html


def _page(title, paras, links=(), mailtos=()):
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paras)
    nav = "".join(f'<a href="{h}">{html.escape(t)}</a> ' for h, t in links)
    mt = "".join(f'<p><a href="mailto:{e}">{html.escape(lbl)}</a></p>' for e, lbl in mailtos)
    return f"<html><head><title>{html.escape(title)}</title></head><body><nav>{nav}</nav><h1>{html.escape(title)}</h1>{body}{mt}</body></html>"


NAV = [("/about", "About"), ("/community", "Community"), ("/students", "Students"), ("/partners", "Partners"),
       ("/contact", "Contact"), ("/careers", "Careers")]


def _co(name, domain, tagline, industry, tags, snippet, pages):
    return {"name": name, "domain": domain, "tagline": tagline, "industry": industry, "tags": tags, "snippet": snippet,
            "pages": pages}


COMPANIES = [
    _co("NimbusForge Cloud", "nimbusforge.example", "Cloud platform for developers", "Cloud platform",
        ["cloud", "developer", "platform", "serverless", "student", "hackathon", "india"],
        "NimbusForge is a cloud platform for developers with student credits and community programs.",
        {"/": _page("NimbusForge Cloud", [
            "NimbusForge is a cloud platform that gives developers managed compute, storage and a serverless runtime.",
            "We launched NimbusForge Edge Functions in August 2026 to run code close to users.",
            "Our developer community runs more than 40 local meetups worldwide."], NAV),
         "/about": _page("About NimbusForge", [
             "We have engineering offices in Bengaluru and Gurugram in the Delhi NCR region.",
             "Aarav Mehta, Head of Developer Relations, leads our community and student programs."], NAV),
         "/students": _page("NimbusForge for Students", [
             "NimbusForge for Students gives university students free cloud credits and a hands-on learning path.",
             "Our campus ambassador program supports student clubs at colleges across India."], NAV),
         "/community": _page("Community", [
             "NimbusForge sponsored four student hackathons in India in 2025, including events in Delhi and Pune.",
             "Our developer relations team hosts workshops and open-source office hours every month."], NAV),
         "/partners": _page("Partners", ["Interested in partnering with us on events? Write to partnerships@nimbusforge.example."], NAV,
                            [("partnerships@nimbusforge.example", "partnerships@nimbusforge.example")]),
         "/contact": _page("Contact", ["General enquiries: hello@nimbusforge.example", "Developer community: devrel@nimbusforge.example"], NAV,
                           [("hello@nimbusforge.example", "hello@nimbusforge.example"), ("devrel@nimbusforge.example", "devrel@nimbusforge.example")]),
         "/careers": _page("Careers", ["We are hiring cloud engineers and developer advocates in Gurugram."], NAV)}),

    _co("Tensorloom AI", "tensorloom.example", "Model APIs for builders", "AI / ML API",
        ["ai", "ml", "api", "developer", "student", "hackathon", "startup", "india"],
        "Tensorloom provides AI model APIs and runs a student program with API credits.",
        {"/": _page("Tensorloom AI", [
            "Tensorloom provides hosted machine learning model APIs for developers and startups.",
            "In June 2026 we introduced Tensorloom Studio, a notebook environment for fine-tuning models.",
            "Customers in India use Tensorloom to build chat and vision features."], NAV),
         "/students": _page("Tensorloom for Students", [
             "Tensorloom for Students offers university students free API credits every semester.",
             "Student developers can join our community program to get early access to new models."], NAV),
         "/community": _page("Community", [
             "Tensorloom sponsored three university hackathons in 2025 and provided API credits to every team.",
             "We host a developer community forum and monthly demo days."], NAV),
         "/about": _page("About", ["Riya Sen, Founder, started Tensorloom to make model APIs simple for builders."], NAV),
         "/contact": _page("Contact", ["Community and partnerships: community@tensorloom.example", "Founder: riya@tensorloom.example"], NAV,
                           [("community@tensorloom.example", "community@tensorloom.example"), ("riya@tensorloom.example", "Riya Sen")])}),

    _co("QueryDock", "querydock.example", "Open-source database", "Database",
        ["database", "developer", "open-source", "cloud", "student"],
        "QueryDock is an open-source database with a developer community.",
        {"/": _page("QueryDock", [
            "QueryDock is an open-source database that gives developers fast analytics on their own data.",
            "We announced QueryDock Cloud in March 2026 as a managed service."], NAV),
         "/community": _page("Community", [
             "QueryDock sponsored developer meetups and a regional tech conference last year.",
             "Our open-source community has contributors from many countries."], NAV),
         "/about": _page("About", ["Our team works remotely with a small office in Bengaluru."], NAV),
         "/partners": _page("Sponsorship", ["For event sponsorship requests write to sponsorship@querydock.example."], NAV,
                            [("sponsorship@querydock.example", "sponsorship@querydock.example")])}),

    _co("ShieldKernel Security", "shieldkernel.example", "Application security", "Cybersecurity",
        ["cybersecurity", "security", "developer", "student", "university"],
        "ShieldKernel builds application security tooling and supports university security clubs.",
        {"/": _page("ShieldKernel Security", [
            "ShieldKernel provides application security tools that help developers find vulnerabilities early.",
            "We released ShieldKernel Scan 2.0 with support for serverless apps."], NAV),
         "/community": _page("Community", [
             "ShieldKernel supports university security clubs and sponsors capture-the-flag events for students.",
             "Our research team publishes open-source security tooling."], NAV),
         "/partners": _page("Partners", ["Partnership enquiries: partnerships@shieldkernel.example"], NAV,
                            [("partnerships@shieldkernel.example", "partnerships@shieldkernel.example")])}),

    _co("LearnLattice", "learnlattice.example", "Online learning for engineers", "EdTech",
        ["edtech", "student", "campus", "university", "india", "delhi", "learning"],
        "LearnLattice is an edtech platform with a campus ambassador program in India.",
        {"/": _page("LearnLattice", [
            "LearnLattice offers online courses that help engineering students build job-ready skills.",
            "Our headquarters is in New Delhi and we work with colleges across India."], NAV),
         "/students": _page("Campus", [
             "The LearnLattice campus ambassador program works with student clubs at universities in Delhi NCR.",
             "We partner with universities to run bootcamps and sponsor student tech festivals."], NAV),
         "/contact": _page("Contact", ["Campus programs: campus@learnlattice.example"], NAV,
                           [("campus@learnlattice.example", "campus@learnlattice.example")])}),

    _co("PayPebble", "paypebble.example", "Payments for young professionals", "FinTech",
        ["fintech", "student", "payments", "india", "developer", "api"],
        "PayPebble offers payment APIs and student accounts in India.",
        {"/": _page("PayPebble", [
            "PayPebble provides payment APIs that help developers accept UPI and card payments.",
            "PayPebble student accounts give college students a zero-fee account."], NAV),
         "/community": _page("Community", [
             "PayPebble sponsored two college fests in India in 2025 as part of its student outreach.",
             "Developers can join our community Slack for integration help."], NAV),
         "/contact": _page("Contact", ["Brand and events: marketing@paypebble.example"], NAV,
                           [("marketing@paypebble.example", "marketing@paypebble.example")])}),

    _co("BoltBoard Devices", "boltboard.example", "Developer boards for makers", "Hardware / IoT",
        ["hardware", "iot", "developer", "student", "university", "india"],
        "BoltBoard makes developer boards used in university labs across India.",
        {"/": _page("BoltBoard Devices", [
            "BoltBoard builds low-cost developer boards for students and makers.",
            "Our boards are distributed in India through partners in Delhi and Bengaluru."], NAV),
         "/students": _page("Education", [
             "BoltBoard partners with university labs to supply kits for student projects.",
             "We offer an education discount program for colleges."], NAV),
         "/contact": _page("Contact", ["Write to us at hello@boltboard.example"], NAV, [("hello@boltboard.example", "hello@boltboard.example")])}),

    _co("DeployDen Hosting", "deployden.example", "Hosting for side projects", "Hosting",
        ["hosting", "cloud", "developer", "student", "hackathon"],
        "DeployDen is a hosting platform with a free tier popular at hackathons.",
        {"/": _page("DeployDen Hosting", [
            "DeployDen is a hosting platform that lets developers deploy apps in one command.",
            "DeployDen sponsored hackathons in Europe and North America with free tier upgrades."], NAV),
         "/contact": _page("Contact", ["Please use the contact form on this page to reach our team."], NAV)}),

    _co("ByteBloom", "bytebloom.example", "Early-stage product studio", "Startup",
        ["startup", "developer", "hiring", "student", "delhi"],
        "ByteBloom is an early-stage startup in Delhi hiring student developers.",
        {"/": _page("ByteBloom", [
            "ByteBloom is an early-stage startup building productivity tools for small teams.",
            "We are hiring student developers and interns in Delhi."], NAV),
         "/about": _page("About", ["Karan Verma, Founder, runs ByteBloom from Delhi NCR."], NAV),
         "/contact": _page("Contact", ["Reach the founder at karan@bytebloom.example"], NAV, [("karan@bytebloom.example", "Karan Verma")])}),

    _co("Harbor & Pine Furniture", "harborpine.example", "Handmade furniture", "Furniture retail",
        ["furniture", "india", "retail"],
        "Harbor & Pine sells handmade wooden furniture across India.",
        {"/": _page("Harbor & Pine Furniture", ["Harbor & Pine sells handmade wooden furniture."], NAV)}),
]

PAGES_BY_URL = {}
for _c in COMPANIES:
    for _path, _html in _c["pages"].items():
        PAGES_BY_URL[f"https://www.{_c['domain']}{_path}"] = _html
        PAGES_BY_URL[f"https://{_c['domain']}{_path}"] = _html
