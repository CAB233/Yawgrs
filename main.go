package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	C "github.com/sagernet/sing-box/constant"
	"github.com/sagernet/sing-box/log"
	"github.com/sagernet/sing-box/option"
	"github.com/sagernet/sing/common"
	E "github.com/sagernet/sing/common/exceptions"

	"github.com/google/go-github/v45/github"
	"github.com/v2fly/v2ray-core/v5/app/router/routercommon"
	"google.golang.org/protobuf/proto"
)

const RuleSetVersion = 3

var githubClient *github.Client

func init() {
	accessToken, loaded := os.LookupEnv("ACCESS_TOKEN")
	if !loaded {
		githubClient = github.NewClient(nil)
		return
	}
	transport := &github.BasicAuthTransport{
		Username: accessToken,
	}
	githubClient = github.NewClient(transport.Client())
}

func fetch(from string) (*github.RepositoryRelease, error) {
	names := strings.SplitN(from, "/", 2)
	latestRelease, _, err := githubClient.Repositories.GetLatestRelease(context.Background(), names[0], names[1])
	if err != nil {
		return nil, err
	}
	return latestRelease, err
}

func get(downloadURL *string) ([]byte, error) {
	log.Info("download ", *downloadURL)
	response, err := http.Get(*downloadURL)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	return io.ReadAll(response.Body)
}

func download(release *github.RepositoryRelease) ([]byte, error) {
	geositeAsset := common.Find(release.Assets, func(it *github.ReleaseAsset) bool {
		return *it.Name == "geosite.dat"
	})
	geositeChecksumAsset := common.Find(release.Assets, func(it *github.ReleaseAsset) bool {
		return *it.Name == "geosite.dat.sha256sum"
	})
	if geositeAsset == nil {
		return nil, E.New("geosite asset not found in upstream release ", release.Name)
	}
	if geositeChecksumAsset == nil {
		return nil, E.New("geosite checksum asset not found in upstream release ", release.Name)
	}
	data, err := get(geositeAsset.BrowserDownloadURL)
	if err != nil {
		return nil, err
	}
	remoteChecksum, err := get(geositeChecksumAsset.BrowserDownloadURL)
	if err != nil {
		return nil, err
	}
	checksum := sha256.Sum256(data)
	if hex.EncodeToString(checksum[:]) != string(remoteChecksum[:64]) {
		return nil, E.New("checksum mismatch")
	}
	return data, nil
}

type DomainItem struct {
	Type  int
	Value string
}

type SourceRuleSet struct {
	Version int                   `json:"version"`
	Rules   []option.HeadlessRule `json:"rules"`
}

func parse(vGeositeData []byte) (map[string][]DomainItem, error) {
	vGeositeList := routercommon.GeoSiteList{}
	err := proto.Unmarshal(vGeositeData, &vGeositeList)
	if err != nil {
		return nil, err
	}
	domainMap := make(map[string][]DomainItem)
	for _, vGeositeEntry := range vGeositeList.Entry {
		code := strings.ToLower(vGeositeEntry.CountryCode)
		domains := make([]DomainItem, 0, len(vGeositeEntry.Domain)*2)
		attributes := make(map[string][]*routercommon.Domain)
		for _, domain := range vGeositeEntry.Domain {
			if len(domain.Attribute) > 0 {
				for _, attribute := range domain.Attribute {
					attributes[attribute.Key] = append(attributes[attribute.Key], domain)
				}
			}
			switch domain.Type {
			case routercommon.Domain_Plain:
				domains = append(domains, DomainItem{
					Type:  1,
					Value: domain.Value,
				})
			case routercommon.Domain_Regex:
				domains = append(domains, DomainItem{
					Type:  2,
					Value: domain.Value,
				})
			case routercommon.Domain_RootDomain:
				domains = append(domains, DomainItem{
					Type:  3,
					Value: domain.Value,
				})
			case routercommon.Domain_Full:
				domains = append(domains, DomainItem{
					Type:  0,
					Value: domain.Value,
				})
			}
		}
		domainMap[code] = common.Uniq(domains)
		for attribute, attributeEntries := range attributes {
			attributeDomains := make([]DomainItem, 0, len(attributeEntries)*2)
			for _, domain := range attributeEntries {
				switch domain.Type {
				case routercommon.Domain_Plain:
					attributeDomains = append(attributeDomains, DomainItem{
						Type:  1,
						Value: domain.Value,
					})
				case routercommon.Domain_Regex:
					attributeDomains = append(attributeDomains, DomainItem{
						Type:  2,
						Value: domain.Value,
					})
				case routercommon.Domain_RootDomain:
					attributeDomains = append(attributeDomains, DomainItem{
						Type:  3,
						Value: domain.Value,
					})
				case routercommon.Domain_Full:
					attributeDomains = append(attributeDomains, DomainItem{
						Type:  0,
						Value: domain.Value,
					})
				}
			}
			domainMap[code+"@"+attribute] = common.Uniq(attributeDomains)
		}
	}
	return domainMap, nil
}

func compile(items []DomainItem) (domain, domainSuffix, domainKeyword, domainRegex []string) {
	for _, item := range items {
		switch item.Type {
		case 0:
			domain = append(domain, item.Value)
		case 1:
			domainKeyword = append(domainKeyword, item.Value)
		case 2:
			domainRegex = append(domainRegex, item.Value)
		case 3:
			domainSuffix = append(domainSuffix, item.Value)
		}
	}
	return
}

func generate(release *github.RepositoryRelease, ruleSetOutput string) error {
	vData, err := download(release)
	if err != nil {
		return err
	}
	domainMap, err := parse(vData)
	if err != nil {
		return err
	}

	os.RemoveAll(ruleSetOutput)
	err = os.MkdirAll(ruleSetOutput, 0o755)
	if err != nil {
		return err
	}

	for code, domains := range domainMap {
		var headlessRule option.DefaultHeadlessRule
		headlessRule.Domain, headlessRule.DomainSuffix, headlessRule.DomainKeyword, headlessRule.DomainRegex = compile(domains)

		ruleSet := SourceRuleSet{
			Version: RuleSetVersion,
			Rules: []option.HeadlessRule{
				{
					Type:           C.RuleTypeDefault,
					DefaultOptions: headlessRule,
				},
			},
		}

		jsonPath, _ := filepath.Abs(filepath.Join(ruleSetOutput, "geosite-"+code+".json"))
		log.Info("write ", jsonPath)
		outputFile, err := os.Create(jsonPath)
		if err != nil {
			return err
		}
		je := json.NewEncoder(outputFile)
		je.SetEscapeHTML(false)
		je.SetIndent("", "  ")
		err = je.Encode(ruleSet)
		if err != nil {
			outputFile.Close()
			return err
		}
		outputFile.Close()
	}

	return nil
}

func release(source string, destination string, ruleSetOutput string) error {
	sourceRelease, err := fetch(source)
	if err != nil {
		return err
	}
	destinationRelease, err := fetch(destination)
	if err != nil {
		log.Warn("missing destination latest release")
	} else {
		if os.Getenv("NO_SKIP") != "true" && strings.Contains(*destinationRelease.Name, *sourceRelease.Name) {
			log.Info("already latest")
			return nil
		}
	}
	err = generate(sourceRelease, ruleSetOutput)
	if err != nil {
		return err
	}
	return nil
}

func main() {
	err := release(
		"Loyalsoldier/v2ray-rules-dat",
		"lyc8503/sing-geosite",
		"rule-set",
	)
	if err != nil {
		log.Fatal(err)
	}
}
